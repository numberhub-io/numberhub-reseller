"""Selling numbers to members, and keeping each order in step with NumberHub.

Money rules (tested in tests/run_tests.py):
  * Buying holds the member's price and creates the order in one transaction;
    only then is the number bought on the reseller's NumberHub account. A
    definite "no" from NumberHub gives the hold back. A lost or unclear reply
    keeps the hold and the order BUYING; the recovery sweep asks again with the
    same Idempotency-Key, so a number is never bought twice.
  * The member's price is the reseller's markup on NumberHub's CEILING for the
    route (`price_max`), the most NumberHub can charge the reseller for it, so a
    sale can never cost the reseller more than the member paid.
  * When the NumberHub order ends, the member's hold is charged if a code arrived
    and released if not — exactly once (repo.settle).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from app import repo
from app.config import settings
from app.models import Member, Order, Reseller
from app.numberhub import NumberHub, NumberHubError, TransportError, authoritative, dec, flag

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


async def known_service(reseller: Reseller, code: str) -> bool:
    """A code the catalog lists. Unknown codes (an old or forged button) never
    cost a NumberHub request. True while the catalog can't be loaded."""
    items = await services(reseller)
    return not items or any(s["code"] == code for s in items)


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
    if not await known_service(reseller, service):
        return []
    try:
        rows = await cli.countries(service)
    except NumberHubError as exc:
        raise _map_error(exc, reseller) from exc
    out = []
    for r in rows:
        ceiling = dec(r.get("price_max") or r.get("price"))
        if ceiling <= 0:
            continue
        ceiling = max(ceiling, learned_ceiling(reseller.id, service, str(r.get("country"))))
        out.append({**r, "ceiling": ceiling, "member_price": member_price(ceiling, reseller.markup_pct),
                    "emoji": flag(r.get("flag"))})
    out.sort(key=_quality_key)
    _country_cache[key] = (time.monotonic(), out)
    # "from $x" only from routes a member would actually be offered first.
    good = [r["member_price"] for r in out if r.get("in_stock") and not r.get("rate_dead")]
    stocked = good or [r["member_price"] for r in out if r.get("in_stock")] or [r["member_price"] for r in out]
    if stocked:
        _from_price[key] = (time.monotonic(), min(stocked))
    return out


# Cheapest member price per (reseller, service), for "💬 WhatsApp · $0.24+".
_from_price: dict[tuple[int, str], tuple[float, Decimal]] = {}
FROM_PRICE_TTL = 900


def from_price(reseller_id: int, service: str) -> Decimal | None:
    hit = _from_price.get((reseller_id, service))
    return hit[1] if hit and time.monotonic() - hit[0] < FROM_PRICE_TTL else None


async def warm_popular(reseller: Reseller) -> None:
    """Keep the popular apps' "from" prices fresh (one request every 0.5 s, well
    inside the API's 60 per 10 s), so the service grid shows them instantly."""
    from app.catalog_ui import POPULAR
    for code, _icon, _name in POPULAR:
        hit = _from_price.get((reseller.id, code))
        if hit and time.monotonic() - hit[0] < 600:
            continue
        try:
            await countries(reseller, code, fresh=True)
        except SellError as exc:
            if exc.reason == "paused":
                return              # the key or the wallet is the problem, not the app
        except Exception:  # noqa: BLE001
            log.debug("warm %s/%s failed", reseller.id, code)
        await asyncio.sleep(0.5)


def invalidate_prices(reseller_id: int) -> None:
    for k in [k for k in _country_cache if k[0] == reseller_id]:
        _country_cache.pop(k, None)


def forget_prices(reseller_id: int) -> None:
    """The commission changed: no screen may show a price from the old one."""
    invalidate_prices(reseller_id)
    for k in [k for k in _from_price if k[0] == reseller_id]:
        _from_price.pop(k, None)


def prune_caches() -> None:
    """Drop what has expired, so memory stays flat with many shops and apps."""
    now = time.monotonic()
    for k in [k for k, (ts, _) in _country_cache.items() if now - ts > 600]:
        _country_cache.pop(k, None)
    for k in [k for k, (ts, _) in _from_price.items() if now - ts > FROM_PRICE_TTL]:
        _from_price.pop(k, None)
    for k in [k for k, (ts, _) in _learned.items() if now - ts > LEARNED_TTL]:
        _learned.pop(k, None)
    for k in [k for k, ts in _owner_warned.items() if now - ts > 7200]:
        _owner_warned.pop(k, None)


# What NumberHub really reserved when it refused a buy as `price_exceeded`. The
# list can lag or under-report a route's top price (it did on live NumberHub
# from 2026-09-23 to 10-02: every thin route was refused), so the refusal's own
# price wins over the list for a while. Otherwise the member would see "price
# changed", tap again and be refused again, forever.
_learned: dict[tuple[int, str, str], tuple[float, Decimal]] = {}
LEARNED_TTL = 900


def learned_ceiling(reseller_id: int, service: str, country: str) -> Decimal:
    hit = _learned.get((reseller_id, service, str(country)))
    return hit[1] if hit and time.monotonic() - hit[0] < LEARNED_TTL else Decimal("0")


def learn_ceiling(reseller_id: int, service: str, country: str, price) -> None:
    value = dec(price)
    if value > 0:
        _learned[(reseller_id, service, str(country))] = (time.monotonic(), value)
        _country_cache.pop((reseller_id, service), None)


# ─── owner alerts ────────────────────────────────────────────────────────────
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


def _key_problem(exc: NumberHubError) -> str | None:
    """Which owner alert a 401/403 means (None: not a key problem)."""
    if exc.status == 401:
        return "bad_key"
    if exc.status == 403:
        return "key_ip" if exc.code == "ip_not_allowed" else "key_scope"
    return None


def _map_error(exc: NumberHubError, reseller: Reseller) -> SellError:
    code = exc.code
    if code == "insufficient_funds":
        asyncio.ensure_future(warn_owner(reseller, "low_balance"))
        return SellError("paused")
    if code == "daily_spend_limit_exceeded":
        asyncio.ensure_future(warn_owner(reseller, "daily_limit"))
        return SellError("paused")
    problem = _key_problem(exc)
    if problem:
        asyncio.ensure_future(warn_owner(reseller, problem))
        return SellError("paused")
    if code in ("sold_out", "purchase_failed", "unknown_service", "operators_unavailable"):
        return SellError("sold_out")
    if code == "price_exceeded":
        return SellError("price_changed", member_price(dec(exc.data.get("price")), reseller.markup_pct))
    if code == "duplicate_order":
        # NumberHub's cap of open orders per route counts the whole account (all
        # shops), not this member: the route is busy, not the member's fault.
        asyncio.ensure_future(warn_owner(reseller, "route_cap"))
        return SellError("busy")
    if code == "rate_limited" or isinstance(exc, TransportError):
        return SellError("busy")
    return SellError("failed")


# ─── buying ──────────────────────────────────────────────────────────────────
_inflight: set[int] = set()          # orders a live buy() or recovery is talking to NumberHub about
_members_buying: set[int] = set()    # one purchase at a time per member (a double tap)
_orphans: dict[int, tuple[int, float]] = {}   # NumberHub order -> (reseller, next try): give it back
BUYING_GIVE_UP_SEC = 20 * 3600       # NumberHub keeps a key 24 h: never replay past that


async def buy(reseller: Reseller, member: Member, service: str, country: str,
              shown_price: Decimal | None = None) -> Order:
    if reseller.status != Reseller.ACTIVE:
        raise SellError("paused")
    if member.is_blocked:
        raise SellError("blocked")
    if member.id in _members_buying:
        raise SellError("duplicate")
    _members_buying.add(member.id)
    try:
        return await _buy(reseller, member, service, str(country), shown_price)
    finally:
        _members_buying.discard(member.id)


async def _buy(reseller: Reseller, member: Member, service: str, country: str,
               shown_price: Decimal | None) -> Order:
    if await repo.member_open_count(member.id) >= settings.max_open_per_member:
        raise SellError("too_many_open")
    if await repo.member_open_count(member.id, service, country) >= settings.max_open_per_route:
        raise SellError("too_many_open")
    cli = client_for(reseller.id)
    if cli is None:
        raise SellError("paused")
    if not await known_service(reseller, service):
        raise SellError("sold_out")
    # A member without the credit costs no NumberHub request (the cached price
    # is enough to know that); everyone else gets a fresh price.
    cached = _country_cache.get((reseller.id, service))
    if cached:
        row0 = next((r for r in cached[1] if str(r["country"]) == country), None)
        if row0 is not None and member.available < row0["member_price"]:
            raise SellError("no_credit", row0["member_price"])
    rows = await countries(reseller, service, fresh=True)
    row = next((r for r in rows if str(r["country"]) == country), None)
    if row is None:
        raise SellError("sold_out")
    price = row["member_price"]
    if shown_price is not None and price > shown_price:
        raise SellError("price_changed", price)
    from app.catalog_ui import nice_name
    svc_name = nice_name(service, service_name(await services(reseller), service))
    order = await repo.create_order_with_hold(
        reseller_id=reseller.id, member_id=member.id, service=service, service_name=svc_name,
        country=country, country_name=row.get("name"), country_iso=(row.get("flag") or None),
        member_price=price, markup_pct_at_buy=reseller.markup_pct)
    if order is None:
        raise SellError("no_credit", price)
    _inflight.add(order.id)
    try:
        try:
            nh = await cli.buy(service, country, row["ceiling"], idempotency_key(order))
        except TransportError:
            # Outcome unknown even after retries: the row stays BUYING with its
            # hold and the recovery sweep asks again with the same key.
            log.warning("order %s: purchase outcome unknown — recovery will finish it", order.id)
            raise SellError("processing")
        except NumberHubError as exc:
            # NumberHub's purchase handler said no: nothing was bought.
            await repo.fail_order(order.id)
            if exc.code == "price_exceeded":
                learn_ceiling(reseller.id, service, country, exc.data.get("price"))
            raise _map_error(exc, reseller) from exc
        except Exception:
            # e.g. the HTTP client was closed under us. The request may have gone
            # out, so the same as a lost reply: keep the hold, let recovery ask.
            log.exception("order %s: purchase crashed — recovery will finish it", order.id)
            raise SellError("processing")
        if not await _attach(reseller, order.id, nh):
            raise SellError("failed")
    finally:
        _inflight.discard(order.id)
    invalidate_prices(reseller.id)
    log.info("reseller %s member %s bought order %s (nh %s) %s/%s for %s (ceiling %s)",
             reseller.id, member.id, order.id, nh.get("id"), service, country, price, row["ceiling"])
    return await repo.get_order(order.id)


async def _attach(reseller: Reseller, order_id: int, nh: dict) -> bool:
    """Record NumberHub's order on ours. If ours was given up on meanwhile (FAILED,
    hold released), take the hold again; if the member can't cover it any more,
    give the number back at NumberHub so no code arrives that nobody pays for."""
    if await repo.link_order(order_id, nh):
        return True
    cur = await repo.get_order(order_id)
    if cur is not None and cur.nh_id == int(nh["id"]):
        return True                  # the other path (buy or recovery) linked it already
    if await repo.revive_failed(order_id, nh):
        log.warning("order %s: a purchase given up on exists after all (nh %s); hold taken again",
                    order_id, nh.get("id"))
        return True
    log.error("order %s: NumberHub order %s exists but the member no longer covers it — giving it back",
              order_id, nh.get("id"))
    _orphans[int(nh["id"])] = (reseller.id, 0.0)
    await release_orphans()
    return False


async def release_orphans() -> None:
    """Cancel NumberHub orders that no member order holds money for (retried
    until NumberHub's cancel lock opens)."""
    now = time.monotonic()
    for nh_id, (rid, due) in list(_orphans.items()):
        if due > now:
            continue
        cli = client_for(rid)
        if cli is None:
            continue
        try:
            body = await cli.cancel(nh_id)
        except NumberHubError as exc:
            if exc.code == "cancel_locked":
                _orphans[nh_id] = (rid, now + int(exc.data.get("seconds_remaining") or 15) + 3)
            elif exc.status == 404:
                _orphans.pop(nh_id, None)
            else:
                _orphans[nh_id] = (rid, now + 30)
            continue
        _orphans.pop(nh_id, None)
        log.warning("NumberHub order %s given back (%s)", nh_id,
                    "cancelled" if body.get("ok") else body.get("reason") or "already closed")


def idempotency_key(order: Order) -> str:
    """Unique per order across databases. NumberHub keeps a key for 24 h per API
    key, and the same key can sit in two databases (a test copy, a restored
    backup), where order ids repeat. With only the ids, the live test's second
    run collided with the first ("nhr-1-1-v1" -> idempotency_conflict), and an
    identical body would have replayed the OLD order and its old code. The
    creation time to the microsecond, read back from the database, is the same
    on every retry and never repeats."""
    stamp = order.created_at.strftime("%Y%m%d%H%M%S%f") if order.created_at else "0"
    return f"nhr-{order.reseller_id}-{order.id}-{stamp}"


def original_ceiling(member_price_: Decimal, markup_pct: Decimal) -> Decimal:
    return (Decimal(member_price_) / (1 + Decimal(markup_pct) / 100)).quantize(CENT, rounding=ROUND_FLOOR)


def _age_sec(order: Order) -> float:
    created = order.created_at
    if created is None:
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - created).total_seconds()


async def recover_buying() -> None:
    """Finish purchases whose reply was lost or unclear (crash, timeout, 429):
    ask again with the same Idempotency-Key — NumberHub replays the original
    result, or makes the purchase if the first request never arrived."""
    for order in await repo.stale_buying(older_than_sec=60):
        if order.id in _inflight:
            continue                    # a live buy() is still on it
        if _age_sec(order) > BUYING_GIVE_UP_SEC:
            # Past NumberHub's key retention a replay would be a NEW purchase.
            # Any number from back then has long expired: give the hold back.
            if await repo.fail_order(order.id):
                log.warning("order %s: no answer from NumberHub for 20 h; member hold released", order.id)
            continue
        reseller = await repo.get_reseller(order.reseller_id)
        cli = client_for(order.reseller_id)
        if reseller is None or cli is None:
            continue
        # The replay needs the SAME request body. The original ceiling comes back
        # exactly from the member price: ceil(c*k)/k floored to cents == c.
        ceiling = original_ceiling(order.member_price, order.markup_pct_at_buy)
        _inflight.add(order.id)
        try:
            nh = await cli.buy(order.service, order.country, ceiling, idempotency_key(order))
        except TransportError:
            continue                    # still unclear: try again next sweep
        except NumberHubError as exc:
            # Only the purchase handler's own answer proves there is no order.
            # 429, 401/403 and the idempotency gate say nothing about it.
            if authoritative(exc):
                await repo.fail_order(order.id)
            continue
        finally:
            _inflight.discard(order.id)
        if await _attach(reseller, order.id, nh):
            log.info("order %s: recovered purchase (nh %s)", order.id, nh.get("id"))
            await _show_recovered(reseller, order.id)


async def _show_recovered(reseller: Reseller, order_id: int) -> None:
    """The member was told "processing": send them the number now."""
    from app import runtime, texts
    bot = runtime.bot_for(reseller.id)
    order = await repo.get_order(order_id)
    member = await repo.get_member(order.member_id) if order else None
    if bot is None or member is None:
        return
    text, kb = texts.order_card(member, order)
    try:
        msg = await bot.send_message(member.telegram_id, text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:  # noqa: BLE001 — the member blocked the bot
        return
    await repo.set_card(order.id, msg.chat.id, msg.message_id)


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
_offlist_polled: dict[int, float] = {}
_backoff_until: dict[int, float] = {}
CARD_TICK_SEC = 20
OFFLIST_PER_PASS = 4          # GET /numbers/{id} per pass for orders past the list's 100


async def sync_reseller(reseller: Reseller) -> None:
    """One pass: mirror NumberHub's state of this reseller's live orders, settle
    the ended ones, tell members about codes, and refresh their cards. One list
    call covers the newest 100 orders of the account; older live ones are polled
    a few per pass, so the key stays inside its 60 requests / 10 s."""
    now = time.monotonic()
    if _backoff_until.get(reseller.id, 0) > now:
        return
    live = await repo.open_orders(reseller.id) + await repo.recently_received(reseller.id)
    if not live:
        _track(reseller.id, set())
        return
    cli = client_for(reseller.id)
    if cli is None:
        return
    try:
        listed = {int(o["id"]): o for o in await cli.orders(limit=100) if o.get("id") is not None}
    except NumberHubError as exc:
        await _sync_trouble(reseller, exc)
        return
    from app import runtime, texts
    bot = runtime.bot_for(reseller.id)
    missing = sorted((o for o in live if o.nh_id not in listed), key=lambda o: _offlist_polled.get(o.id, 0))
    polled = {o.id for o in missing[:OFFLIST_PER_PASS]}
    for order in live:
        nh = listed.get(order.nh_id)
        if nh is None:
            if order.id not in polled:
                continue
            _offlist_polled[order.id] = now
            try:
                nh = await cli.number(order.nh_id)
            except NumberHubError as exc:
                if exc.status in (401, 403, 429):
                    await _sync_trouble(reseller, exc)
                    return
                continue
        before = order.status
        changed, n_codes = await repo.apply_nh_state(order.id, nh)
        fresh = await repo.get_order(order.id)
        if fresh is None:
            continue
        await settle_order(fresh)
        fresh = await repo.get_order(order.id)
        member = await repo.get_member(fresh.member_id)
        if bot is None or member is None:
            continue
        codes = order_codes(fresh)
        new_codes = n_codes > fresh.codes_announced
        if new_codes:
            announced = fresh.codes_announced
            for code in codes[fresh.codes_announced:]:
                if await send_ex(bot, member.telegram_id, texts.code_arrived(member, fresh, code)) == "retry":
                    break               # Telegram is busy: send the rest next pass
                announced += 1
            await repo.set_codes_announced(fresh.id, announced)
        elif changed and before == "pending" and fresh.status == "waiting":
            await send(bot, member.telegram_id, texts.number_ready(member, fresh))
        elif changed and fresh.status in ("canceled", "expired") and not codes:
            await send(bot, member.telegram_id, texts.refunded(member, fresh))
        due = time.monotonic() - _last_card_edit.get(fresh.id, 0) >= CARD_TICK_SEC
        if changed or new_codes or due:
            await refresh_card(bot, member, fresh)
    _track(reseller.id, {o.id for o in live})


_order_reseller: dict[int, int] = {}


def _track(reseller_id: int, live_ids: set[int]) -> None:
    """Forget the per-order bookkeeping of orders that left the live set."""
    for oid in [k for k, rid in _order_reseller.items() if rid == reseller_id and k not in live_ids]:
        _order_reseller.pop(oid, None)
        _last_card_edit.pop(oid, None)
        _offlist_polled.pop(oid, None)
    for oid in live_ids:
        _order_reseller[oid] = reseller_id


async def _sync_trouble(reseller: Reseller, exc: NumberHubError) -> None:
    """The key can't read orders right now: back off instead of hammering."""
    now = time.monotonic()
    if exc.status == 429:
        _backoff_until[reseller.id] = now + 15
    elif exc.status in (401, 403):
        _backoff_until[reseller.id] = now + 60
        problem = _key_problem(exc)
        if exc.status == 401 and reseller.status == Reseller.ACTIVE:
            await repo.update_reseller(reseller.id, status=Reseller.KEY_INVALID)
            log.warning("reseller %s: NumberHub rejected the API key; sales paused until a new key", reseller.id)
        if problem:
            await warn_owner(reseller, problem)


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
    if order.status in Order.TERMINAL:
        _last_card_edit.pop(order.id, None)


async def settle_sweep() -> None:
    for order in await repo.unsettled_ended():
        await settle_order(order)


async def send_ex(bot, chat_id: int, text: str, reply_markup=None) -> str:
    """'ok', 'gone' (blocked the bot / chat gone: stop trying) or 'retry'."""
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
    try:
        await bot.send_message(chat_id, text, reply_markup=reply_markup, disable_web_page_preview=True)
        return "ok"
    except (TelegramForbiddenError, TelegramBadRequest):
        return "gone"
    except Exception:  # noqa: BLE001 — flood limit, network: worth another try
        return "retry"


async def send(bot, chat_id: int, text: str, reply_markup=None) -> bool:
    return await send_ex(bot, chat_id, text, reply_markup) == "ok"


async def broadcast(bot, reseller_id: int, text: str) -> tuple[int, int]:
    sent = failed = 0
    for chat_id in await repo.member_chat_ids(reseller_id):
        if await send(bot, chat_id, text):
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)      # stay under Telegram's ~30 messages/s
    return sent, failed
