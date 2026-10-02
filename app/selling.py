"""Selling numbers to members, and keeping each order in step with NumberHub.

Money rules (tested in tests/test_selling.py):
  * Buying holds the member's price on their balance first; only then is the
    number bought on the reseller's NumberHub account. Every failure gives the
    hold back. A lost reply is retried with the same Idempotency-Key, so it can
    never buy twice.
  * The member's price is the reseller's markup on NumberHub's CEILING for the
    route (`price_max`), the most NumberHub can charge the reseller for it, so a
    sale can never cost the reseller more than the member paid.
  * When the NumberHub order ends, the member's hold is charged if a code arrived
    and released if not — exactly once (repo.settle).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from app import repo
from app.config import settings
from app.models import Member, Order, Reseller
from app.numberhub import NumberHub, NumberHubError, TransportError, dec, flag

log = logging.getLogger(__name__)
CENT = Decimal("0.01")


class SellError(Exception):
    def __init__(self, reason: str, price: Decimal | None = None, seconds: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.price = price
        self.seconds = seconds


# ─── API clients per reseller (set by runtime) ───────────────────────────────
_clients: dict[int, NumberHub] = {}


def set_client(reseller_id: int, client: NumberHub) -> None:
    _clients[reseller_id] = client


def client_for(reseller_id: int) -> NumberHub | None:
    return _clients.get(reseller_id)


def drop_client(reseller_id: int) -> NumberHub | None:
    return _clients.pop(reseller_id, None)


# ─── prices / catalog ────────────────────────────────────────────────────────
def member_price(ceiling: Decimal, markup_pct: Decimal) -> Decimal:
    return (Decimal(ceiling) * (1 + Decimal(markup_pct) / 100)).quantize(CENT, rounding=ROUND_CEILING)


_svc_cache: tuple[float, list[dict]] = (0.0, [])
_country_cache: dict[tuple[int, str], tuple[float, list[dict]]] = {}
SERVICES_TTL = 600
COUNTRIES_TTL = 30


async def services(reseller: Reseller) -> list[dict]:
    global _svc_cache
    if time.monotonic() - _svc_cache[0] < SERVICES_TTL and _svc_cache[1]:
        return _svc_cache[1]
    cli = client_for(reseller.id)
    if cli is None:
        return _svc_cache[1]
    try:
        items = [s for s in await cli.services() if s.get("code") and s.get("name")]
    except NumberHubError:
        return _svc_cache[1]
    _svc_cache = (time.monotonic(), items)
    return items


def service_name(items: list[dict], code: str) -> str:
    return next((s["name"] for s in items if s["code"] == code), code)


async def search_services(reseller: Reseller, query: str, limit: int = 12) -> list[dict]:
    q = (query or "").strip().lower()
    if not q:
        return []
    items = await services(reseller)
    starts = [s for s in items if s["name"].lower().startswith(q) or s["code"].lower() == q]
    contains = [s for s in items if q in s["name"].lower() and s not in starts]
    return (starts + contains)[:limit]


def _quality_key(r: dict):
    # In stock and delivering first, then in stock but weak, out of stock, dead.
    if r.get("rate_dead"):
        tier = 3
    elif not r.get("in_stock"):
        tier = 2
    elif r.get("rate_low") or r.get("collapsed"):
        tier = 1
    else:
        tier = 0
    rate = r.get("rate")
    return (tier, -(rate if rate is not None else 25), r["member_price"], r.get("name") or "")


async def countries(reseller: Reseller, service: str, fresh: bool = False) -> list[dict]:
    key = (reseller.id, service)
    hit = _country_cache.get(key)
    if hit and not fresh and time.monotonic() - hit[0] < COUNTRIES_TTL:
        return hit[1]
    cli = client_for(reseller.id)
    if cli is None:
        raise SellError("paused")
    try:
        rows = await cli.countries(service)
    except NumberHubError as exc:
        raise _map_error(exc, reseller) from exc
    out = []
    for r in rows:
        ceiling = dec(r.get("price_max") or r.get("price"))
        if ceiling <= 0:
            continue
        out.append({**r, "ceiling": ceiling, "member_price": member_price(ceiling, reseller.markup_pct),
                    "emoji": flag(r.get("flag"))})
    out.sort(key=_quality_key)
    _country_cache[key] = (time.monotonic(), out)
    return out


def invalidate_prices(reseller_id: int) -> None:
    for k in [k for k in _country_cache if k[0] == reseller_id]:
        _country_cache.pop(k, None)


# ─── buying ──────────────────────────────────────────────────────────────────
_owner_warned: dict[tuple[int, str], float] = {}


async def warn_owner(reseller: Reseller, reason: str) -> None:
    """Tell the reseller (in their own bot) why sales stopped — once an hour."""
    key = (reseller.id, reason)
    now = time.monotonic()
    if now - _owner_warned.get(key, float("-inf")) < 3600:
        return
    _owner_warned[key] = now
    from app import runtime, texts
    bot = runtime.bot_for(reseller.id)
    if bot is not None:
        await send(bot, reseller.owner_id, texts.owner_alert(reason))


def _map_error(exc: NumberHubError, reseller: Reseller) -> SellError:
    code = exc.code
    if code == "insufficient_funds":
        asyncio.ensure_future(warn_owner(reseller, "low_balance"))
        return SellError("paused")
    if code == "daily_spend_limit_exceeded":
        asyncio.ensure_future(warn_owner(reseller, "daily_limit"))
        return SellError("paused")
    if exc.status in (401, 403):
        asyncio.ensure_future(warn_owner(reseller, "bad_key"))
        return SellError("paused")
    if code in ("sold_out", "purchase_failed", "unknown_service", "operators_unavailable"):
        return SellError("sold_out")
    if code == "price_exceeded":
        return SellError("price_changed", member_price(dec(exc.data.get("price")), reseller.markup_pct))
    if code == "duplicate_order":
        return SellError("too_many_open")
    if code == "rate_limited" or isinstance(exc, TransportError):
        return SellError("busy")
    return SellError("failed")


async def buy(reseller: Reseller, member: Member, service: str, country: str,
              shown_price: Decimal | None = None) -> Order:
    if reseller.status != Reseller.ACTIVE:
        raise SellError("paused")
    if member.is_blocked:
        raise SellError("blocked")
    if await repo.member_open_count(member.id) >= settings.max_open_per_member:
        raise SellError("too_many_open")
    if await repo.member_open_count(member.id, service, country) >= settings.max_open_per_route:
        raise SellError("too_many_open")
    cli = client_for(reseller.id)
    if cli is None:
        raise SellError("paused")
    rows = await countries(reseller, service, fresh=True)
    row = next((r for r in rows if str(r["country"]) == str(country)), None)
    if row is None:
        raise SellError("sold_out")
    price = row["member_price"]
    if shown_price is not None and price > shown_price:
        raise SellError("price_changed", price)
    if not await repo.member_try_hold(member.id, price):
        raise SellError("no_credit", price)
    svc_name = service_name(await services(reseller), service)
    order = await repo.create_order(reseller_id=reseller.id, member_id=member.id, service=service,
                                    service_name=svc_name, country=str(country),
                                    country_name=row.get("name"), country_iso=(row.get("flag") or None),
                                    member_price=price, markup_pct_at_buy=reseller.markup_pct)
    try:
        nh = await cli.buy(service, str(country), row["ceiling"], idempotency_key(order))
    except TransportError:
        # Outcome unknown even after retries: the row stays BUYING and the
        # recovery sweep asks again with the same key (replay or a clean miss).
        log.warning("order %s: purchase reply lost — recovery will finish it", order.id)
        raise SellError("processing")
    except NumberHubError as exc:
        await repo.fail_order(order.id)
        raise _map_error(exc, reseller) from exc
    except Exception:
        await repo.fail_order(order.id)
        raise
    await repo.link_order(order.id, nh)
    invalidate_prices(reseller.id)
    log.info("reseller %s member %s bought order %s (nh %s) %s/%s for %s (ceiling %s)",
             reseller.id, member.id, order.id, nh.get("id"), service, country, price, row["ceiling"])
    return await repo.get_order(order.id)


def idempotency_key(order: Order) -> str:
    return f"nhr-{order.reseller_id}-{order.id}-v1"


def original_ceiling(member_price_: Decimal, markup_pct: Decimal) -> Decimal:
    return (Decimal(member_price_) / (1 + Decimal(markup_pct) / 100)).quantize(CENT, rounding=ROUND_FLOOR)


async def recover_buying() -> None:
    """Finish purchases whose reply was lost (crash, timeout): ask again with the
    same Idempotency-Key — NumberHub replays the original result."""
    for order in await repo.stale_buying(older_than_sec=60):
        reseller = await repo.get_reseller(order.reseller_id)
        cli = client_for(order.reseller_id)
        if reseller is None or cli is None:
            continue
        # The replay needs the SAME request body. The original ceiling comes back
        # exactly from the member price: ceil(c*k)/k floored to cents == c.
        ceiling = original_ceiling(order.member_price, order.markup_pct_at_buy)
        try:
            nh = await cli.buy(order.service, order.country, ceiling, idempotency_key(order))
        except TransportError:
            continue                    # still unreachable: try again next sweep
        except NumberHubError as exc:
            if exc.code == "idempotency_in_progress":
                continue                # the original is still running at NumberHub
            await repo.fail_order(order.id)   # a definite answer: no order exists
            continue
        await repo.link_order(order.id, nh)
        log.info("order %s: recovered purchase (nh %s)", order.id, nh.get("id"))


async def cancel(reseller: Reseller, member: Member, order_id: int) -> tuple[bool, str | None, int]:
    """Member cancels their own order. Returns (cancelled, reason, seconds)."""
    order = await repo.get_order(order_id)
    if order is None or order.member_id != member.id or order.nh_id is None:
        return False, "not_found", 0
    if order.status not in ("pending", "waiting"):
        return False, "closed", 0
    cli = client_for(reseller.id)
    if cli is None:
        return False, "busy", 0
    try:
        body = await cli.cancel(order.nh_id)
    except NumberHubError as exc:
        if exc.code == "cancel_locked":
            return False, "locked", int(exc.data.get("seconds_remaining") or 15)
        if exc.status == 404:
            return False, "not_found", 0
        return False, "busy", 0
    if body.get("number"):
        await repo.apply_nh_state(order.id, body["number"])
    fresh = await repo.get_order(order.id)
    await settle_order(fresh)
    if body.get("reason") == "code_received":
        return False, "code_received", 0
    return bool(body.get("ok")), None if body.get("ok") else "closed", 0


# ─── order sync ──────────────────────────────────────────────────────────────
def order_codes(order: Order) -> list[str]:
    try:
        return [str(c) for c in json.loads(order.codes or "[]") if c]
    except (TypeError, ValueError):
        return []


async def settle_order(order: Order | None) -> bool:
    if order is None or order.status in Order.OPEN or order.settled != Order.UNSETTLED:
        return False
    charge = order.status in Order.CHARGED_STATUSES or bool(order_codes(order))
    done = await repo.settle(order.id, charge)
    if done:
        log.info("order %s settled: member %s %s", order.id, "charged" if charge else "refunded",
                 order.member_price)
    return done


_last_card_edit: dict[int, float] = {}
CARD_TICK_SEC = 20


async def sync_reseller(reseller: Reseller) -> None:
    """One pass: mirror NumberHub's state of this reseller's live orders, settle
    the ended ones, tell members about codes, and refresh their cards. One list
    call covers up to 100 orders (the API allows 60 requests / 10 s per key)."""
    live = await repo.open_orders(reseller.id) + await repo.recently_received(reseller.id)
    if not live:
        return
    cli = client_for(reseller.id)
    if cli is None:
        return
    try:
        listed = {int(o["id"]): o for o in await cli.orders(limit=100) if o.get("id") is not None}
    except NumberHubError as exc:
        if exc.status in (401, 403):
            await warn_owner(reseller, "bad_key")
        return
    from app import runtime, texts
    bot = runtime.bot_for(reseller.id)
    for order in live:
        nh = listed.get(order.nh_id)
        if nh is None:
            try:
                nh = await cli.number(order.nh_id)
            except NumberHubError:
                continue
        before = order.status
        changed, n_codes = await repo.apply_nh_state(order.id, nh)
        fresh = await repo.get_order(order.id)
        if fresh is None:
            continue
        await settle_order(fresh)
        fresh = await repo.get_order(order.id)
        member = await repo.get_member(fresh.member_id)
        if bot is not None and member is not None:
            codes = order_codes(fresh)
            if n_codes > fresh.codes_announced:
                for code in codes[fresh.codes_announced:]:
                    await send(bot, member.telegram_id, texts.code_arrived(member, fresh, code))
                await repo.set_codes_announced(fresh.id, n_codes)
            elif changed and before == "pending" and fresh.status == "waiting":
                await send(bot, member.telegram_id, texts.number_ready(member, fresh))
            elif changed and fresh.status in ("canceled", "expired") and not codes:
                await send(bot, member.telegram_id, texts.refunded(member, fresh))
            due = time.monotonic() - _last_card_edit.get(fresh.id, 0) >= CARD_TICK_SEC
            if changed or n_codes or due:
                await refresh_card(bot, member, fresh)


async def refresh_card(bot, member: Member, order: Order) -> None:
    if not order.chat_id or not order.message_id:
        return
    from app import texts
    text, kb = texts.order_card(member, order)
    _last_card_edit[order.id] = time.monotonic()
    try:
        await bot.edit_message_text(text, chat_id=order.chat_id, message_id=order.message_id,
                                    reply_markup=kb, disable_web_page_preview=True)
    except Exception:  # noqa: BLE001 — unchanged / deleted / too old
        pass
    if order.status not in Order.OPEN and order.status != "received":
        _last_card_edit.pop(order.id, None)


async def settle_sweep() -> None:
    for order in await repo.unsettled_ended():
        await settle_order(order)


async def send(bot, chat_id: int, text: str, reply_markup=None) -> bool:
    try:
        await bot.send_message(chat_id, text, reply_markup=reply_markup, disable_web_page_preview=True)
        return True
    except Exception:  # noqa: BLE001 — the member blocked the bot / chat gone
        return False


async def broadcast(bot, reseller_id: int, text: str) -> tuple[int, int]:
    sent = failed = 0
    for chat_id in await repo.member_chat_ids(reseller_id):
        if await send(bot, chat_id, text):
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)      # stay under Telegram's ~30 messages/s
    return sent, failed
