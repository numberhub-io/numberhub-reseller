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
import re
import time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from app import repo
from app.config import settings
from app.models import Member, Order, PriceRule, Reseller
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


async def key_rejected(reseller_id: int, cli: NumberHub) -> None:
    """NumberHub answered 401 to `cli`: the key was revoked (or its account
    blocked). Park the shop as KEY_INVALID (sales paused, no more calls with that
    key) until the owner connects a new one; a reconnect that already stored
    another key is left alone. Before, only the order sync noticed, and it runs
    only while a shop has live orders: a shop with a revoked key and none asked
    for prices with it every 2 minutes for days (1,280 refused calls in 38 h,
    2026-10-15)."""
    from app import crypto
    shop = await repo.get_reseller(reseller_id)
    if shop is None or shop.status != Reseller.ACTIVE:
        return
    try:
        stored = crypto.decrypt(shop.api_key_enc)
    except Exception:  # noqa: BLE001 — undecryptable: runtime.start already refuses this shop
        return
    if cli.uses(stored) and await repo.mark_key_invalid(reseller_id, shop.api_key_enc):
        log.warning("reseller %s: NumberHub rejected the API key; sales paused until a new key", reseller_id)


# ─── prices / catalog ────────────────────────────────────────────────────────
def member_price(ceiling: Decimal, markup_pct: Decimal) -> Decimal:
    return (Decimal(ceiling) * (1 + Decimal(markup_pct) / 100)).quantize(CENT, rounding=ROUND_CEILING)


async def price_rules(reseller_id: int) -> dict[tuple[str, str], PriceRule]:
    return {(r.service, r.country): r for r in await repo.price_rules(reseller_id)}


# What a shop sells, and where each product's own commission is kept (numbers use
# the shop's commission itself, markup_pct).
PRODUCTS = {"sms": None, "email": "markup_email", "proxy": "markup_proxy", "rent": "markup_rent",
            "esim": "markup_esim"}


def own_commission(reseller: Reseller, kind: str) -> Decimal | None:
    """The commission the owner set for this product alone, or None when it has none."""
    field = PRODUCTS.get(kind)
    value = getattr(reseller, field, None) if field else None
    return Decimal(value) if value is not None else None


def commission(reseller: Reseller, kind: str = "sms") -> Decimal:
    """The percent this shop adds to NumberHub's price of one product: the product's
    own, or the commission for numbers while its owner has set none for it.

    Until 2026-11-25 a shop had one commission for everything. It is chosen for
    numbers that cost a few cents: live shops ran up to 300%, which sold a $9.00
    rental for $36.00."""
    own = own_commission(reseller, kind)
    return own if own is not None else Decimal(reseller.markup_pct)


def shop_price(reseller: Reseller, rules: dict, service: str, country: str, ceiling, kind: str = "sms") -> Decimal:
    """What a member pays for one number of this app in this country, or for one
    item of another product (`kind`: an email code, a data package, a rental, an eSIM).

    The owner's most specific rule wins (this app in this country, then this app
    everywhere), else the commission; a percent price is then held to the profit
    cap. A fixed price never goes below NumberHub's price for the number: when
    NumberHub's price rises above it the number sells at NumberHub's price, so the
    owner earns nothing on it but never pays for a customer's number.

    A product with its own commission is priced by that commission alone. The cap
    is "the most you earn on one number": it keeps holding numbers, and a product
    that follows the numbers' commission, exactly as before."""
    ceiling = Decimal(ceiling)
    rule = rules.get((service, str(country))) or rules.get((service, ""))
    if rule is not None and rule.mode == PriceRule.FIXED:
        return max(Decimal(rule.value), ceiling).quantize(CENT, rounding=ROUND_CEILING)
    own = own_commission(reseller, kind)
    price = member_price(ceiling, Decimal(rule.value) if rule is not None else commission(reseller, kind))
    cap = getattr(reseller, "max_profit", None)
    if cap is not None and own is None:
        price = min(price, (ceiling + Decimal(cap)).quantize(CENT, rounding=ROUND_CEILING))
    return price


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
    # NumberHub's own order when the API sends it (`rank`, 1 = best): it weighs the
    # route's record against the app's average, so a messenger route delivering 34%
    # leads one with no results at all. Older servers: our own approximation.
    rank = r.get("rank")
    if isinstance(rank, int) and not isinstance(rank, bool):
        return (0, rank, r["member_price"], r.get("name") or "")
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
    return (1 + tier, -(rate if rate is not None else 25), r["member_price"], r.get("name") or "")


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
        if exc.status == 401:
            await key_rejected(reseller.id, cli)
        raise _map_error(exc, reseller) from exc
    rules = await price_rules(reseller.id)
    out = []
    for r in rows:
        ceiling = dec(r.get("price_max") or r.get("price"))
        if ceiling <= 0:
            continue
        ceiling = max(ceiling, learned_ceiling(reseller.id, service, str(r.get("country"))))
        out.append({**r, "ceiling": ceiling,
                    "member_price": shop_price(reseller, rules, service, str(r.get("country")), ceiling),
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


def price_example(reseller_id: int, service: str) -> tuple[Decimal, Decimal] | None:
    """(NumberHub's price, the member's price) of this app's cheapest in-stock
    country, from the cached list: the admin panel's worked example, true with
    custom prices and the cap (the commission alone can't rebuild it)."""
    hit = _country_cache.get((reseller_id, service))
    if not hit:
        return None
    rows = [r for r in hit[1] if r.get("in_stock") and not r.get("rate_dead")] or hit[1]
    if not rows:
        return None
    best = min(rows, key=lambda r: r["member_price"])
    return best["ceiling"], best["member_price"]


async def warm_popular(reseller: Reseller) -> None:
    """Keep the popular apps' "from" prices fresh (one request every 0.5 s, well
    inside the API's 60 per 10 s), so the service grid shows them instantly.
    Selling shops only: a paused one, one waiting for a new key or token, or a
    suspended one has no customers browsing it."""
    if reseller.status != Reseller.ACTIVE:
        return
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
    _proxy_pkgs.pop(reseller_id, None)
    # Email prices were left out until 2026-11-25: they stayed at the old commission
    # until their list ran out by itself.
    for k in [k for k in _email_domains if k[0] == reseller_id]:
        _email_domains.pop(k, None)
    for k in [k for k in _rent_services if k[0] == reseller_id]:
        _rent_services.pop(k, None)
    for k in [k for k in _esim_plans if k[0] == reseller_id]:
        _esim_plans.pop(k, None)
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
    if code in ("sold_out", "purchase_failed", "unknown_service", "operators_unavailable", "unknown_package",
                "invalid_country", "invalid_duration", "changed", "unknown_region"):
        return SellError("sold_out")
    if code == "price_exceeded":
        return SellError("price_changed", member_price(dec(exc.data.get("price")), reseller.markup_pct))
    if code == "duplicate_order":
        # NumberHub's cap of open orders per route counts the whole account (all
        # shops), not this member: the route is busy, not the member's fault.
        asyncio.ensure_future(warn_owner(reseller, "route_cap"))
        return SellError("busy")
    if code in ("rate_limited", "rent_unavailable", "esim_busy", "esim_disabled") or isinstance(exc, TransportError):
        return SellError("busy")
    return SellError("failed")


# ─── buying ──────────────────────────────────────────────────────────────────
_owner_locks: dict[int, asyncio.Lock] = {}   # purchases with hand-added balance: one at a time per owner


def by_hand_part(member: Member, price: Decimal) -> Decimal:
    """How much of `price` this customer would pay with balance that was added by
    hand. Paid-in money goes first, and their open orders are counted as spending
    it already."""
    paid_free = max(Decimal("0"), Decimal(member.paid_left or 0) - Decimal(member.held or 0))
    return max(Decimal("0"), Decimal(price) - paid_free)


async def _backed(reseller: Reseller, cli, share: Decimal, ceiling: Decimal) -> bool | None:
    """Does the owner's OWN money in the NumberHub wallet cover this purchase's
    hand-added share? Their own money is what the wallet has available beyond what
    the customers of all their shops paid in and still hold. None = the wallet
    could not be read: nothing proves the money is there, so nothing is bought."""
    try:
        wallet = dec((await cli.balance()).get("available"))
    except NumberHubError:
        return None
    own = wallet - await repo.owner_paid_left(reseller.owner_id)
    return own >= (Decimal(ceiling) * share).quantize(Decimal("0.01"), rounding="ROUND_UP")


_inflight: set[int] = set()          # orders a live buy() or recovery is talking to NumberHub about
_members_buying: set[int] = set()    # one purchase at a time per member (a double tap)
_orphans: dict[int, tuple[int, float, str]] = {}   # NumberHub order -> (reseller, next try, kind): give it back
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


# A number that gets no code costs its buyer nothing, so nothing slows a customer (or a
# script) that takes one after another. NumberHub judges the OWNER's whole account on
# how many numbers get a code, and pauses new numbers for all of that owner's shops when
# a day holds 150 with under 3%. One customer must not use that up. Sized on 30 days of
# all shops (2026-11-24: 646 numbers by 39 customers): the most numbers with no code at
# all in one customer's day was 21, and the busiest honest day was 72 numbers with 6 codes.
# The number a customer was about to buy when their balance fell short, kept for a day:
# "money added" then leads straight back to it. Most customers who open a top-up never
# come back to the purchase (the wallet's own bot: 333 of 426 in two weeks, 2026-10).
RESUME_SEC = 24 * 3600
_resume: dict[int, tuple[float, str, str, str, str]] = {}      # member -> (when, app, country, name, flag)


def remember_route(member_id: int, service: str, country: str, name: str, emoji: str) -> None:
    if len(_resume) > 5000:
        cutoff = time.monotonic() - RESUME_SEC
        for k in [k for k, v in _resume.items() if v[0] < cutoff]:
            _resume.pop(k, None)
    _resume[member_id] = (time.monotonic(), str(service), str(country), str(name or service), str(emoji or ""))


def remembered_route(member_id: int) -> tuple[str, str, str, str] | None:
    hit = _resume.get(member_id)
    if hit is None or time.monotonic() - hit[0] > RESUME_SEC:
        _resume.pop(member_id, None)
        return None
    return hit[1:]


CYCLE_NO_CODE = 30              # numbers in 24 h that ended with no code, when none got one
CYCLE_MANY = 100                # ...or this many, when under CYCLE_PCT percent got one
CYCLE_PCT = 3
_cycler_told: dict[tuple[int, int], float] = {}


def cycling(no_code: int, got: int) -> bool:
    """Whether a day of this customer's numbers reads as cycling, not as buying."""
    if got == 0:
        return no_code >= CYCLE_NO_CODE
    return no_code + got >= CYCLE_MANY and got * 100 < CYCLE_PCT * (no_code + got)


async def _tell_owner_cycler(reseller: Reseller, member: Member, no_code: int, got: int) -> None:
    """Once a day per customer, in the owner's own bot."""
    key, now = (reseller.id, member.id), time.monotonic()
    if now - _cycler_told.get(key, float("-inf")) < 86400:
        return
    _cycler_told[key] = now
    from app import runtime, texts
    bot = runtime.bot_for(reseller.id)
    if bot is not None:
        who = f"@{texts.esc(member.username)} " if member.username else ""
        await send(bot, reseller.owner_id, texts.owner_alert("number_cycler").format(
            who=who, id=member.telegram_id, n=no_code + got, got=got))


async def _buy(reseller: Reseller, member: Member, service: str, country: str,
               shown_price: Decimal | None) -> Order:
    no_code, got = await repo.number_outcomes(member.id, 24)
    if cycling(no_code, got):
        asyncio.ensure_future(_tell_owner_cycler(reseller, member, no_code, got))
        raise SellError("number_limit")
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
    values = dict(reseller_id=reseller.id, member_id=member.id, service=service, service_name=svc_name,
                  country=country, country_name=row.get("name"), country_iso=(row.get("flag") or None),
                  member_price=price, markup_pct_at_buy=reseller.markup_pct, ceiling_at_buy=row["ceiling"])

    async def repriced(exc: NumberHubError) -> Decimal:
        learn_ceiling(reseller.id, service, country, exc.data.get("price"))
        # The member is shown THIS route's price at NumberHub's real price
        # (the owner's rule or cap included), not the plain commission.
        return shop_price(reseller, await price_rules(reseller.id), service, country, dec(exc.data.get("price")))

    order = await _backed_place(reseller, member, cli, values,
                                lambda o: cli.buy(service, country, row["ceiling"], idempotency_key(o)), repriced)
    invalidate_prices(reseller.id)
    _resume.pop(member.id, None)        # bought: nothing to come back to
    return order


async def _backed_place(reseller: Reseller, member: Member, cli, values: dict, purchase, repriced) -> Order:
    """Place a purchase of any kind. A balance the owner added by hand has no money
    behind it in the NumberHub wallet unless the owner put some there. It must not
    buy with what OTHER customers paid in. Such purchases go one at a time per
    owner, through to NumberHub's answer, so several at once cannot each see the
    same free money."""
    price, ceiling = values["member_price"], values["ceiling_at_buy"]
    fresh = await repo.get_member(member.id) or member
    hand = min(price, by_hand_part(fresh, price))
    # Nothing to protect while no customer of this owner holds paid-in money: then
    # the wallet is all the owner's, and NumberHub itself says when it is empty.
    if hand <= 0 or await repo.owner_paid_left(reseller.owner_id) <= 0:
        return await _place(reseller, member, values, purchase, repriced)
    async with _owner_locks.setdefault(reseller.owner_id, asyncio.Lock()):
        backed = await _backed(reseller, cli, hand / price, ceiling)
        if backed is None:
            raise SellError("busy")
        if not backed:
            asyncio.ensure_future(warn_owner(reseller, "by_hand_unbacked"))
            raise SellError("paused")
        return await _place(reseller, member, values, purchase, repriced)


async def _place(reseller: Reseller, member: Member, values: dict, purchase, repriced) -> Order:
    """Hold the customer's price with the order, then make the purchase
    (`purchase(order)` is the NumberHub call, with the order's own key)."""
    price = values["member_price"]
    order = await repo.create_order_with_hold(**values)
    if order is None:
        raise SellError("no_credit", price)
    _inflight.add(order.id)
    try:
        try:
            nh = await purchase(order)
        except TransportError:
            # Outcome unknown even after retries: the row stays BUYING with its
            # hold and the recovery sweep asks again with the same key.
            log.warning("order %s: purchase outcome unknown — recovery will finish it", order.id)
            raise SellError("processing")
        except NumberHubError as exc:
            # NumberHub's purchase handler said no: nothing was bought.
            await repo.fail_order(order.id, exc.code)
            if exc.code == "price_exceeded":
                raise SellError("price_changed", await repriced(exc)) from exc
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
    log.info("reseller %s member %s bought order %s (nh %s) %s/%s for %s (ceiling %s)",
             reseller.id, member.id, order.id, nh.get("id"), values["service"], values["country"], price,
             values["ceiling_at_buy"])
    return await repo.get_order(order.id)


# ─── email codes ─────────────────────────────────────────────────────────────
# A mailbox that receives one mail from one site. Same money rules as a number:
# held with the order, charged once when the mail arrives, released otherwise.
# Our mail source judges the whole NumberHub account on how many mailboxes get a
# mail, and NumberHub limits each account (80 new mailboxes an hour, a pause on
# low delivery). One careless customer must not use up the owner's limit:
EMAIL_MAX_OPEN = 3              # mailboxes one customer may have waiting
EMAIL_PER_HOUR = 10             # ...and may ask for in an hour
EMAIL_SHOP_PER_HOUR = 50        # all customers of one shop together
EMAIL_WASTED_MAX = 6            # mailboxes in a day that ended with no mail, when none got one
_email_sites: dict[int, tuple[float, list[dict]]] = {}
_email_domains: dict[tuple[int, str], tuple[float, list[dict]]] = {}


def email_on(reseller: Reseller) -> bool:
    """Email codes are sold unless the owner switched them off."""
    return getattr(reseller, "email_on", None) != 0


async def email_sites(reseller: Reseller) -> list[dict]:
    """The sites most asked for, for the picker's buttons: [{site, name}]. Empty
    when the shop's API has none or cannot be asked (the button then hides)."""
    hit = _email_sites.get(reseller.id)
    if hit and time.monotonic() - hit[0] < 3600:
        return hit[1]
    cli = client_for(reseller.id)
    if cli is None:
        return []
    try:
        rows = await cli.email_sites()
    except NumberHubError:
        if hit is None:
            # Asked again in a minute, not on every menu (each one waited for the timeout).
            _email_sites[reseller.id] = (time.monotonic() - 3540, [])
        return hit[1] if hit else []
    rows = [{"site": str(r["site"])[:120], "name": str(r.get("name") or r["site"])[:60]} for r in rows]
    _email_sites[reseller.id] = (time.monotonic(), rows)
    return rows


_SITE_CLEAN = re.compile(r"^\s*(?:https?://)?(?:www\.)?([^/\s?#]+)", re.I)
_SITE_VALID = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)*\.[a-z]{2,}$")


def email_site(raw: str) -> str | None:
    """A website a customer typed, as its bare address, or None when it is not one.

    Takes what people paste: "https://Example.com/signup", "www.example.com",
    "@example.com", "name@example.com". A plain word like "example" is not an
    address (it is searched by name instead): without its ending it is a guess, and
    a mailbox opened for a wrong guess cannot be used. A mailbox can be opened for
    ANY site, so an address needs no entry in the list of known sites: until
    2026-11-24 a typed address was answered "nothing found" for ever."""
    txt = str(raw or "").strip().lstrip("@").strip()
    if not txt or len(txt) > 253:
        return None
    m = _SITE_CLEAN.match(txt)
    if not m:
        return None
    host = m.group(1).strip().rstrip(".").lower()
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    return host if _SITE_VALID.match(host) and len(host) <= 120 else None


async def email_search(reseller: Reseller, q: str) -> list[dict]:
    """Sites matching what the customer typed: [{site, name}], at most 8."""
    cli = client_for(reseller.id)
    q = (q or "").strip()[:60]
    if cli is None or len(q) < 2:
        return []
    try:
        rows = await cli.email_suggest(q)
    except NumberHubError:
        return []
    # known False = the address is a guess from the name: it is shown and warned about.
    return [{"site": str(r["site"])[:120], "name": str(r.get("name") or r["site"])[:60],
             "known": r.get("known") is not False} for r in rows][:8]


async def email_domains(reseller: Reseller, site: str, fresh: bool = False) -> list[dict]:
    """The mail domains a customer can pick for `site`, in NumberHub's order (the
    ones with a good delivery record first), in stock only, at the shop's price:
    [{domain, nh_price, member_price, delivery}]. Raises SellError."""
    key = (reseller.id, site)
    hit = _email_domains.get(key)
    if hit and not fresh and time.monotonic() - hit[0] < 60:
        return hit[1]
    cli = client_for(reseller.id)
    if cli is None:
        raise SellError("paused")
    try:
        rows = await cli.email_domains(site)
    except NumberHubError as exc:
        raise _map_error(exc, reseller) from exc
    out = []
    for r in rows:
        price = dec(r.get("price"))
        if price <= 0 or not r.get("in_stock"):
            continue
        out.append({"domain": str(r["domain"])[:120], "nh_price": price,
                    "member_price": shop_price(reseller, {}, site, str(r["domain"]), price, "email"),
                    "delivery": r.get("delivery") if r.get("delivery") in ("high", "low") else None})
    _email_domains[key] = (time.monotonic(), out)
    if len(_email_domains) > 2000:
        for k in [k for k, v in list(_email_domains.items()) if time.monotonic() - v[0] > 300]:
            _email_domains.pop(k, None)
    return out


async def buy_email(reseller: Reseller, member: Member, site: str, site_name_: str, domain: str,
                    shown_price: Decimal | None = None) -> Order:
    if reseller.status != Reseller.ACTIVE or not email_on(reseller):
        raise SellError("paused")
    if member.is_blocked:
        raise SellError("blocked")
    if member.id in _members_buying:
        raise SellError("duplicate")
    _members_buying.add(member.id)
    try:
        return await _buy_email(reseller, member, site, site_name_, domain, shown_price)
    finally:
        _members_buying.discard(member.id)


async def _buy_email(reseller: Reseller, member: Member, site: str, site_name_: str, domain: str,
                     shown_price: Decimal | None) -> Order:
    if await repo.member_open_count(member.id, kind="email") >= EMAIL_MAX_OPEN:
        raise SellError("too_many_open")
    if (await repo.email_orders_since(60, member_id=member.id) >= EMAIL_PER_HOUR
            or await repo.email_orders_since(60, reseller_id=reseller.id) >= EMAIL_SHOP_PER_HOUR):
        raise SellError("email_limit")
    # Mailboxes that are opened and never used count against the mail source for
    # every customer of every shop. Someone whose mailboxes never get a mail stops
    # for the day; one mail arriving lifts it.
    wasted, got = await repo.email_outcomes(member.id, 24)
    if wasted >= EMAIL_WASTED_MAX and got == 0:
        raise SellError("email_limit")
    cli = client_for(reseller.id)
    if cli is None:
        raise SellError("paused")
    row = next((r for r in await email_domains(reseller, site, fresh=True) if r["domain"] == domain), None)
    if row is None:
        raise SellError("sold_out")
    price = row["member_price"]
    if shown_price is not None and price > shown_price:
        raise SellError("price_changed", price)
    values = dict(reseller_id=reseller.id, member_id=member.id, kind="email", service=site,
                  service_name=(site_name_ or site)[:128], country=domain, country_name=domain, country_iso=None,
                  member_price=price, markup_pct_at_buy=commission(reseller, "email"), ceiling_at_buy=row["nh_price"])

    async def repriced(exc: NumberHubError) -> Decimal:
        _email_domains.pop((reseller.id, site), None)
        return shop_price(reseller, {}, site, domain, dec(exc.data.get("price")), "email")

    order = await _backed_place(reseller, member, cli, values,
                                lambda o: cli.buy_email(site, domain, row["nh_price"], idempotency_key(o)), repriced)
    _email_domains.pop((reseller.id, site), None)
    return order


# ─── proxy data packages ─────────────────────────────────────────────────────
# Residential data in GB, used within the package's days. Unlike a number it is
# paid for when bought: the customer is charged as soon as NumberHub confirms the
# package, and nothing is charged when the purchase does not happen. It cannot be
# cancelled or given back.
PROXY_MAX_ACTIVE = 5            # packages one customer may have working at once
_proxy_pkgs: dict[int, tuple[float, list[dict]]] = {}
_proxy_live: dict[int, tuple[float, dict]] = {}       # order id -> NumberHub's last answer, for a few seconds


def proxy_on(reseller: Reseller) -> bool:
    return getattr(reseller, "proxy_on", None) != 0


async def proxy_packages(reseller: Reseller, fresh: bool = False) -> list[dict]:
    """The packages this shop sells, at its own price (NumberHub's plus the shop's
    commission): [{gb, nh_price, member_price, per_gb, popular, days}]. Empty when
    the shop's API has none or cannot be asked (the menu button then hides)."""
    hit = _proxy_pkgs.get(reseller.id)
    if hit and not fresh and time.monotonic() - hit[0] < 300:
        return hit[1]
    cli = client_for(reseller.id)
    if cli is None:
        return []
    try:
        data = await cli.proxy_packages()
    except NumberHubError:
        if hit is None:
            # Asked again in a minute, not on every menu.
            _proxy_pkgs[reseller.id] = (time.monotonic() - 240, [])
        return hit[1] if hit else []
    rows: list[dict] = []
    listed = data.get("packages") if data.get("enabled") else []
    try:
        days = int(data.get("validity_days") or 30)
    except (TypeError, ValueError):
        days = 30
    for p in listed if isinstance(listed, list) else []:
        try:
            gb = int(p.get("gb") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        nh_price = dec(p.get("price"))
        if gb <= 0 or nh_price <= 0:
            continue
        price = shop_price(reseller, {}, "proxy", str(gb), nh_price, "proxy")
        rows.append({"gb": gb, "nh_price": nh_price, "member_price": price, "days": days,
                     "per_gb": (price / gb).quantize(CENT, rounding=ROUND_CEILING), "popular": bool(p.get("popular"))})
    rows.sort(key=lambda r: r["gb"])
    _proxy_pkgs[reseller.id] = (time.monotonic(), rows)
    return rows


async def buy_proxy(reseller: Reseller, member: Member, gb: int, shown_price: Decimal | None = None) -> Order:
    if reseller.status != Reseller.ACTIVE or not proxy_on(reseller):
        raise SellError("paused")
    if member.is_blocked:
        raise SellError("blocked")
    if member.id in _members_buying:
        raise SellError("duplicate")
    _members_buying.add(member.id)
    try:
        return await _buy_proxy(reseller, member, int(gb), shown_price)
    finally:
        _members_buying.discard(member.id)


async def _buy_proxy(reseller: Reseller, member: Member, gb: int, shown_price: Decimal | None) -> Order:
    if await repo.proxy_active_count(member.id) >= PROXY_MAX_ACTIVE:
        raise SellError("proxy_limit")
    cli = client_for(reseller.id)
    if cli is None:
        raise SellError("paused")
    row = next((r for r in await proxy_packages(reseller, fresh=True) if r["gb"] == gb), None)
    if row is None:
        raise SellError("sold_out")
    price = row["member_price"]
    if shown_price is not None and price > shown_price:
        raise SellError("price_changed", price)
    values = dict(reseller_id=reseller.id, member_id=member.id, kind="proxy", service="proxy",
                  service_name=f"{gb} GB", country=str(gb), country_name=f"{gb} GB", country_iso=None,
                  member_price=price, markup_pct_at_buy=commission(reseller, "proxy"), ceiling_at_buy=row["nh_price"])

    async def repriced(exc: NumberHubError) -> Decimal:
        _proxy_pkgs.pop(reseller.id, None)
        return shop_price(reseller, {}, "proxy", str(gb), dec(exc.data.get("price")), "proxy")

    order = await _backed_place(reseller, member, cli, values,
                                lambda o: cli.buy_proxy(gb, row["nh_price"], idempotency_key(o)), repriced)
    # The package works from this moment: it is charged now, not when something arrives.
    await settle_order(await repo.get_order(order.id))
    return await repo.get_order(order.id) or order


async def proxy_live(reseller: Reseller, order: Order) -> dict | None:
    """NumberHub's own view of a package: how to connect, what is used, whether it
    has ended. None when it cannot be read now. Kept for a few seconds, so a
    customer tapping Refresh does not use up the shop's API allowance."""
    if order.kind != "proxy" or order.nh_id is None:
        return None
    hit = _proxy_live.get(order.id)
    if hit and time.monotonic() - hit[0] < 20:
        return hit[1]
    cli = client_for(reseller.id)
    if cli is None:
        return None
    try:
        nh = await cli.proxy(order.nh_id)
    except (NumberHubError, KeyError):
        return None
    if not isinstance(nh, dict) or not isinstance(nh.get("proxy"), dict):
        return None
    if len(_proxy_live) > 2000:
        _proxy_live.clear()
    _proxy_live[order.id] = (time.monotonic(), nh)
    if nh.get("id") is not None:
        await repo.apply_nh_state(order.id, nh)          # "completed" once it has ended
    return nh


# ─── eSIM data plans ─────────────────────────────────────────────────────────
# Mobile data for a destination: the customer picks where and a plan, and installs it
# with one tap or a QR. The owner's NumberHub wallet pays when the plan is ordered. In
# the shop the customer's price is held with the order and charged when NumberHub
# reports the eSIM delivered, which is usually with the purchase itself. If NumberHub
# gives the order up it refunds the wallet, and the hold is released: the same path as
# a number. An eSIM that was delivered cannot be returned. How to install it is read
# from NumberHub when it is shown; the shop's database never holds it.
ESIM_MAX_WAITING = 3            # eSIMs one customer may have being prepared at once
ESIM_PLANS_MAX = 200            # plans listed for one destination (the real lists run to about 100)
ESIM_PUSH_AFTER_SEC = 20        # the purchase itself sends an eSIM that came with it; the sync only later ones
_ESIM_CODE = re.compile(r"^[A-Za-z0-9_.\-]{1,24}$")
_esim_regions: dict[int, tuple[float, list[dict]]] = {}
_esim_plans: dict[tuple[int, str], tuple[float, list[dict]]] = {}
_esim_learned: dict[tuple[int, str, str], tuple[float, Decimal]] = {}     # the price a refused purchase named
_esim_live: dict[int, tuple[float, dict]] = {}


def esim_on(reseller: Reseller) -> bool:
    return getattr(reseller, "esim_on", None) != 0


async def esim_regions(reseller: Reseller) -> list[dict]:
    """Where plans are sold, the most asked for first: [{code, name, iso, emoji}].
    Empty when the shop's API sells none or cannot be asked (the menu button hides)."""
    hit = _esim_regions.get(reseller.id)
    if hit and time.monotonic() - hit[0] < 3600:
        return hit[1]
    cli = client_for(reseller.id)
    if cli is None:
        return []
    try:
        rows = await cli.esim_regions()
    except NumberHubError:
        if hit is None:
            _esim_regions[reseller.id] = (time.monotonic() - 3540, [])      # asked again in a minute
        return hit[1] if hit else []
    out = []
    for r in rows:
        code = str(r["code"])
        if not _ESIM_CODE.match(code):
            continue
        iso = str(r.get("flag") or "").upper()
        iso = iso if len(iso) == 2 and iso.isalpha() else None
        out.append({"code": code, "name": str(r.get("name") or code)[:60], "iso": iso,
                    "emoji": flag(iso) if iso else "🌍"})
    _esim_regions[reseller.id] = (time.monotonic(), out)
    return out


async def esim_plans(reseller: Reseller, region: str, fresh: bool = False) -> list[dict]:
    """The plans for one destination at the shop's price, cheapest first: [{code,
    name, data, days, nh_price, member_price, countries, country_names, active_type,
    region_name}]. Raises SellError."""
    key = (reseller.id, str(region))
    hit = _esim_plans.get(key)
    if hit and not fresh and time.monotonic() - hit[0] < 300:
        return hit[1]
    cli = client_for(reseller.id)
    if cli is None:
        raise SellError("paused")
    try:
        data = await cli.esim_packages(region)
    except NumberHubError as exc:
        if exc.status == 401:
            await key_rejected(reseller.id, cli)
        raise _map_error(exc, reseller) from exc
    listed = data.get("packages")
    out = []
    for p in listed if isinstance(listed, list) else []:
        if not isinstance(p, dict) or not _ESIM_CODE.match(str(p.get("code") or "")):
            continue
        code, price = str(p["code"]), dec(p.get("price"))
        try:
            days = int(p.get("duration") or 0)
        except (TypeError, ValueError):
            days = 0
        if price <= 0 or days <= 0:
            continue
        if str(p.get("unit") or "day").lower().startswith("month"):
            days *= 30
        learned = _esim_learned.get((reseller.id, str(region), code))
        if learned and time.monotonic() - learned[0] < LEARNED_TTL:
            price = max(price, learned[1])
        names = p.get("country_names") if isinstance(p.get("country_names"), list) else []
        try:
            countries = max(1, int(p.get("countries") or 1))
        except (TypeError, ValueError):
            countries = 1
        out.append({"code": code, "name": str(p.get("name") or code)[:80], "data": str(p.get("gb") or "")[:16],
                    "days": days, "nh_price": price, "member_price": shop_price(reseller, {}, "esim", code, price, "esim"),
                    "countries": countries, "country_names": [str(x)[:40] for x in names[:80] if x],
                    "active_type": p.get("active_type") if p.get("active_type") in (1, 2, 3) else None,
                    "region_name": str(data.get("region_name") or "")[:80]})
    out.sort(key=lambda r: (r["member_price"], r["name"]))
    out = out[:ESIM_PLANS_MAX]
    if len(_esim_plans) > 3000:
        for k in [k for k, v in list(_esim_plans.items()) if time.monotonic() - v[0] > 600]:
            _esim_plans.pop(k, None)
    _esim_plans[key] = (time.monotonic(), out)
    return out


async def buy_esim(reseller: Reseller, member: Member, region: str, code: str,
                   shown_price: Decimal | None = None) -> Order:
    if reseller.status != Reseller.ACTIVE or not esim_on(reseller):
        raise SellError("paused")
    if member.is_blocked:
        raise SellError("blocked")
    if member.id in _members_buying:
        raise SellError("duplicate")
    _members_buying.add(member.id)
    try:
        return await _buy_esim(reseller, member, str(region), str(code), shown_price)
    finally:
        _members_buying.discard(member.id)


async def _buy_esim(reseller: Reseller, member: Member, region: str, code: str,
                    shown_price: Decimal | None) -> Order:
    if await repo.member_open_count(member.id, kind="esim") >= ESIM_MAX_WAITING:
        raise SellError("esim_limit")
    cli = client_for(reseller.id)
    if cli is None:
        raise SellError("paused")
    row = next((r for r in await esim_plans(reseller, region, fresh=True) if r["code"] == code), None)
    if row is None:
        raise SellError("sold_out")
    price = row["member_price"]
    if shown_price is not None and price > shown_price:
        raise SellError("price_changed", price)
    place = next((x for x in await esim_regions(reseller) if x["code"] == region), None)
    values = dict(reseller_id=reseller.id, member_id=member.id, kind="esim", service=code,
                  service_name=row["name"][:128], country=region,
                  country_name=(row["region_name"] or (place or {}).get("name") or region)[:128],
                  country_iso=(place or {}).get("iso"), member_price=price,
                  markup_pct_at_buy=commission(reseller, "esim"), ceiling_at_buy=row["nh_price"])

    async def repriced(exc: NumberHubError) -> Decimal:
        # The plan's price is read again when it is ordered and can differ from the
        # list: what the refusal names wins for a while, or the customer would confirm,
        # be refused, and confirm again.
        real = dec(exc.data.get("price"))
        if real > 0:
            _esim_learned[(reseller.id, region, code)] = (time.monotonic(), real)
        _esim_plans.pop((reseller.id, region), None)
        return shop_price(reseller, {}, "esim", code, real, "esim")

    order = await _backed_place(reseller, member, cli, values,
                                lambda o: cli.buy_esim(region, code, row["nh_price"], idempotency_key(o)), repriced)
    # Most eSIMs are delivered with the purchase: then it is charged now.
    await settle_order(await repo.get_order(order.id))
    return await repo.get_order(order.id) or order


def esim_profile(live: dict | None) -> dict:
    """How to install it, from NumberHub's answer: {ac, smdp, matchingId, iccid,
    install}. Empty while there is nothing to install yet, or no answer."""
    p = live.get("esim") if isinstance(live, dict) and isinstance(live.get("esim"), dict) else {}
    return p if str(p.get("ac") or "").upper().startswith("LPA:") else {}


async def esim_live(reseller: Reseller, order: Order) -> dict | None:
    """NumberHub's own view of an eSIM order, with how to install it once it is ready.
    None when it cannot be read now. A ready one never changes and is kept ten minutes;
    one still being prepared a few seconds, so a Refresh tap costs no request each time."""
    if order.kind != "esim" or order.nh_id is None:
        return None
    hit = _esim_live.get(order.id)
    if hit and time.monotonic() - hit[0] < (600 if esim_profile(hit[1]) else 10):
        return hit[1]
    cli = client_for(reseller.id)
    if cli is None:
        return None
    try:
        nh = await cli.esim(order.nh_id)
    except (NumberHubError, KeyError):
        return None
    if not isinstance(nh, dict) or not isinstance(nh.get("esim"), dict):
        return None
    if len(_esim_live) > 2000:
        _esim_live.clear()
    _esim_live[order.id] = (time.monotonic(), nh)
    if nh.get("id") is not None:
        await repo.apply_nh_state(order.id, nh)
        await settle_order(await repo.get_order(order.id))
    return nh


async def send_esim(bot, reseller: Reseller, member: Member, order: Order) -> bool:
    """The eSIM is ready: its QR and its card go to the customer. The QR is drawn here
    from the install code, so no picture comes from anywhere else. False = not sent
    (asked again on a later pass)."""
    from aiogram.types import BufferedInputFile
    from app import payments, texts
    from app.i18n import t
    live = await esim_live(reseller, order)
    ac = str(esim_profile(live).get("ac") or "")
    if not ac:
        return False                    # delivered, but how to install it cannot be read yet
    fresh = await repo.get_order(order.id) or order
    text, kb = texts.esim_card(member, fresh, live)
    try:
        await bot.send_photo(member.telegram_id, BufferedInputFile(payments.qr_png(ac), filename="esim.png"),
                             caption=t(member.language or "en", "esim_qr_caption"))
        msg = await bot.send_message(member.telegram_id, text, reply_markup=kb, disable_web_page_preview=True)
    except Exception as exc:  # noqa: BLE001
        from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
        return isinstance(exc, (TelegramForbiddenError, TelegramBadRequest))     # blocked the bot: stop trying
    await repo.set_card(order.id, msg.chat.id, msg.message_id)
    return True


# ─── rented numbers ──────────────────────────────────────────────────────────
# A number that is the customer's for days: every SMS sent to it reaches them. The
# owner's NumberHub wallet pays for it when it is rented. In the shop the customer's
# price is held with the order, like every purchase, and charged as soon as the number
# can no longer be given back: at its first SMS, or when NumberHub's refund time is
# over. NumberHub says on the rental itself until when that is; nothing here guesses
# it from a clock. A cancel that NumberHub confirms inside that time releases the
# hold, so the customer gets back exactly what they paid.
RENT_MAX_ACTIVE = 5             # numbers one customer may be renting at once
RENT_FIRST = ("US", "GB", "DE", "FR", "NL", "CA", "ES", "IT", "ID")     # shown before the other countries
RENT_SERVICES_MAX = 80          # services listed for one country and period
RENT_SYNC_SEC = 15              # how often a shop with nothing but running rentals asks for news
RENT_CANCEL_CONFIRM_SEC = 60    # a cancel seen in the list is believed when it is still there this much later
_RENT_CODE = re.compile(r"^[A-Za-z0-9_]{1,12}$")
_rent_periods: dict[int, tuple[float, list[dict]]] = {}
_rent_countries: dict[tuple[int, int], tuple[float, list[dict]]] = {}
_rent_services: dict[tuple[int, int, str], tuple[float, list[dict]]] = {}
_rent_learned: dict[tuple[int, int, str, str], tuple[float, Decimal]] = {}   # the price a refused purchase named
_rent_cancelling: set[int] = set()            # rentals a customer's own cancel is asking NumberHub about right now
_rent_cancel_seen: dict[int, float] = {}      # rental -> when the list first showed it cancelled
_rent_synced: dict[int, float] = {}


def rent_on(reseller: Reseller) -> bool:
    return getattr(reseller, "rent_on", None) != 0


async def rent_periods(reseller: Reseller) -> list[dict]:
    """The periods a number can be rented for: [{h, label}], shortest first. Empty when
    the shop's API has none or cannot be asked (the menu button then hides)."""
    hit = _rent_periods.get(reseller.id)
    if hit and time.monotonic() - hit[0] < 3600:
        return hit[1]
    cli = client_for(reseller.id)
    if cli is None:
        return []
    try:
        rows = await cli.rent_durations()
    except NumberHubError:
        if hit is None:
            # Asked again in a minute, not on every menu.
            _rent_periods[reseller.id] = (time.monotonic() - 3540, [])
        return hit[1] if hit else []
    out = []
    for r in rows:
        try:
            h = int(r.get("h") or 0)
        except (TypeError, ValueError):
            continue
        if 0 < h <= 24 * 366:
            out.append({"h": h, "label": str(r.get("label") or "")[:40]})
    out.sort(key=lambda r: r["h"])
    _rent_periods[reseller.id] = (time.monotonic(), out)
    return out


async def rent_countries(reseller: Reseller, h: int) -> list[dict]:
    """Countries with numbers to rent for `h` hours: [{country, name, iso, emoji}].
    Raises SellError."""
    key = (reseller.id, int(h))
    hit = _rent_countries.get(key)
    if hit and time.monotonic() - hit[0] < 120:
        return hit[1]
    cli = client_for(reseller.id)
    if cli is None:
        raise SellError("paused")
    try:
        rows = await cli.rent_countries(h)
    except NumberHubError as exc:
        if hit:
            return hit[1]               # the list of a few minutes ago beats "busy"
        raise _map_error(exc, reseller) from exc
    out = []
    for r in rows:
        iso = str(r.get("flag") or "").upper()
        out.append({"country": str(r["country"])[:8], "name": str(r.get("name") or r["country"])[:60],
                    "iso": iso if len(iso) == 2 and iso.isalpha() else None, "emoji": flag(iso)})
    # The list comes in ABC order, which put USA on the second page: the countries asked
    # for most first, then the rest as they came.
    out.sort(key=lambda r: RENT_FIRST.index(r["iso"]) if r["iso"] in RENT_FIRST else len(RENT_FIRST))
    _rent_countries[key] = (time.monotonic(), out)
    return out


async def rent_periods_open(reseller: Reseller) -> list[dict]:
    """The periods that have a country to rent in right now. The list of periods is
    fixed, and the longest often has no number anywhere: offered, it led to an empty
    screen (the real lists on 2026-11-25: 1 month, no country). A period that cannot
    be asked about just now stays listed."""
    out = []
    for p in await rent_periods(reseller):
        try:
            if await rent_countries(reseller, p["h"]):
                out.append(p)
        except SellError:
            out.append(p)
    return out


async def rent_services(reseller: Reseller, h: int, country: str, fresh: bool = False) -> list[dict]:
    """What a number of this country can be rented for, in stock only, at the shop's
    price: [{code, name, nh_price, member_price, popular}]. "Any service" (every SMS)
    first, then "any other", then the popular apps. Raises SellError."""
    key = (reseller.id, int(h), str(country))
    hit = _rent_services.get(key)
    if hit and not fresh and time.monotonic() - hit[0] < 60:
        return hit[1]
    cli = client_for(reseller.id)
    if cli is None:
        raise SellError("paused")
    try:
        rows = await cli.rent_services(h, country)
    except NumberHubError as exc:
        if exc.status == 401:
            await key_rejected(reseller.id, cli)
        raise _map_error(exc, reseller) from exc
    from app.catalog_ui import nice_name
    out = []
    for r in rows:
        code, price = str(r["code"]), dec(r.get("price"))
        if price <= 0 or not r.get("in_stock") or not _RENT_CODE.match(code):
            continue
        learned = _rent_learned.get((reseller.id, int(h), str(country), code))
        if learned and time.monotonic() - learned[0] < LEARNED_TTL:
            price = max(price, learned[1])
        out.append({"code": code, "name": nice_name(code, r.get("name"))[:60], "nh_price": price,
                    "member_price": shop_price(reseller, {}, "rent", code, price, "rent"),
                    "popular": bool(r.get("popular"))})
    out.sort(key=lambda r: 0 if r["code"] == "full" else 1 if r["code"] == "ot" else 2 if r["popular"] else 3)
    out = out[:RENT_SERVICES_MAX]
    if len(_rent_services) > 3000:
        for k in [k for k, v in list(_rent_services.items()) if time.monotonic() - v[0] > 300]:
            _rent_services.pop(k, None)
    _rent_services[key] = (time.monotonic(), out)
    return out


async def buy_rental(reseller: Reseller, member: Member, h: int, country: str, code: str,
                     shown_price: Decimal | None = None) -> Order:
    if reseller.status != Reseller.ACTIVE or not rent_on(reseller):
        raise SellError("paused")
    if member.is_blocked:
        raise SellError("blocked")
    if member.id in _members_buying:
        raise SellError("duplicate")
    _members_buying.add(member.id)
    try:
        return await _buy_rental(reseller, member, int(h), str(country), str(code), shown_price)
    finally:
        _members_buying.discard(member.id)


async def _buy_rental(reseller: Reseller, member: Member, h: int, country: str, code: str,
                      shown_price: Decimal | None) -> Order:
    if await repo.rent_active_count(member.id) >= RENT_MAX_ACTIVE:
        raise SellError("rent_limit")
    cli = client_for(reseller.id)
    if cli is None:
        raise SellError("paused")
    if h not in {p["h"] for p in await rent_periods(reseller)}:
        raise SellError("sold_out")
    row = next((r for r in await rent_services(reseller, h, country, fresh=True) if r["code"] == code), None)
    if row is None:
        raise SellError("sold_out")
    price = row["member_price"]
    if shown_price is not None and price > shown_price:
        raise SellError("price_changed", price)
    try:
        place = next((c for c in await rent_countries(reseller, h) if c["country"] == country), None)
    except SellError:
        place = None                    # only its name and flag: the purchase does not need them
    values = dict(reseller_id=reseller.id, member_id=member.id, kind="rent", service=code,
                  service_name=row["name"][:128], country=country, country_name=(place or {}).get("name") or country,
                  country_iso=(place or {}).get("iso"), member_price=price,
                  markup_pct_at_buy=commission(reseller, "rent"), ceiling_at_buy=row["nh_price"], rent_hours=h)

    async def repriced(exc: NumberHubError) -> Decimal:
        # The list can lag behind what a purchase really costs. The refusal's own price
        # wins for a while, or the customer would confirm, be refused, and confirm again.
        real = dec(exc.data.get("price"))
        if real > 0:
            _rent_learned[(reseller.id, h, country, code)] = (time.monotonic(), real)
        _rent_services.pop((reseller.id, h, country), None)
        return shop_price(reseller, {}, "rent", code, real, "rent")

    order = await _backed_place(reseller, member, cli, values,
                                lambda o: cli.buy_rental(h, country, code, row["nh_price"], idempotency_key(o)),
                                repriced)
    _rent_services.pop((reseller.id, h, country), None)
    return order


def order_messages(order: Order) -> list[dict]:
    """Every SMS a rented number got, oldest first: [{from, text, code}]."""
    try:
        rows = json.loads(getattr(order, "messages", None) or "[]")
    except (TypeError, ValueError):
        return []
    return [m for m in rows if isinstance(m, dict)] if isinstance(rows, list) else []


def rental_due(order: Order) -> bool | None:
    """What to do with a rented number's held price now: True = charge it, False = give
    it back, None = wait.

    NumberHub cancels a rental only together with its refund, so "canceled" gives the
    money back. Anything else that is final, an SMS on the number, or NumberHub no
    longer offering a refund (it sends no `refund_until` then) means the rental was
    used or can no longer be returned: charged. Until then the price stays held: the
    customer cannot spend it, and it is not yet spent either."""
    if order.status in (Order.BUYING, Order.FAILED):
        return None
    if order.status == "canceled":
        return False
    if order.status in Order.TERMINAL or order_messages(order) or order_codes(order):
        return True
    return True if getattr(order, "refund_until", None) is None else None


async def cancel_rental(reseller: Reseller, member: Member, order_id: int) -> tuple[bool, str | None]:
    """The customer gives a rented number back for a refund. Returns (cancelled, why
    not): locked = the refund opens two minutes after the start; closed = too late;
    sms = an SMS arrived; ended = it is over or cancelled already; refused / busy = try
    again.

    NumberHub decides. The hold is released only on its own answer to this cancel."""
    order = await repo.get_order(order_id)
    if order is None or order.member_id != member.id or order.kind != "rent" or order.nh_id is None:
        return False, "not_found"
    if order.status in Order.TERMINAL:
        return False, "ended"
    if order.settled != Order.UNSETTLED:
        return False, "sms" if order_messages(order) else "closed"
    cli = client_for(reseller.id)
    if cli is None or order.id in _rent_cancelling:
        return False, "busy"
    _rent_cancelling.add(order.id)
    try:
        try:
            body = await cli.cancel_rental(order.nh_id)
        except NumberHubError as exc:
            if exc.code == "cancel_window_closed":
                return False, "locked" if exc.data.get("window_state") == "locked" else "closed"
            if exc.status == 404:
                return False, "not_found"
            return False, "busy"        # a lost reply too: the order sync learns what happened
        rental = body.get("rental")
        if isinstance(rental, dict) and rental.get("id") is not None:
            await repo.apply_nh_state(order.id, rental)
        _rent_cancel_seen.pop(order.id, None)
        await settle_order(await repo.get_order(order.id), cancelling=True)
        fresh = await repo.get_order(order.id)
        if body.get("ok") and fresh is not None and fresh.status == "canceled":
            log.info("rental %s (nh %s) cancelled by member %s: %s given back", order.id, order.nh_id, member.id,
                     order.member_price)
            return True, None
        return False, "sms" if fresh is not None and order_messages(fresh) else "refused"
    finally:
        _rent_cancelling.discard(order.id)


async def _rent_not_returned(reseller_id: int, nh_id: int) -> None:
    log.error("rental %s exists at NumberHub, nobody holds money for it and it could not be given back", nh_id)
    shop = await repo.get_reseller(reseller_id)
    if shop is not None:
        await warn_owner(shop, "rent_unpaid")


async def mail_details(reseller: Reseller, order: Order) -> dict:
    """The mail that arrived on an email order: {code, link, message} (any may be
    None). Many sites verify with a link and no code, so the list of orders, which
    carries codes only, is not enough to tell the customer what came."""
    cli = client_for(reseller.id)
    if cli is None or order.nh_id is None:
        return {}
    try:
        block = (await cli.email(order.nh_id)).get("email")
    except (NumberHubError, KeyError):
        return {}
    return block if isinstance(block, dict) else {}


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
    kind = cur.kind if cur is not None else str(nh.get("kind") or "sms")
    if kind in ("proxy", "esim"):
        # A data package or an eSIM is paid for when bought and cannot be given back:
        # the owner has it in their NumberHub account, and nobody paid the shop for it.
        log.error("order %s: %s %s exists but the member no longer covers it", order_id, kind, nh.get("id"))
        asyncio.ensure_future(warn_owner(reseller, kind + "_unpaid"))
        return False
    # A rented number is paid for when rented too, but NumberHub takes it back inside
    # its refund time: tried there, and the owner is told when it cannot be done.
    log.error("order %s: NumberHub order %s exists but the member no longer covers it — giving it back",
              order_id, nh.get("id"))
    _orphans[int(nh["id"])] = (reseller.id, 0.0, kind if kind in ("rent", "email") else "sms")
    await release_orphans()
    return False


async def release_orphans() -> None:
    """Cancel NumberHub orders that no member order holds money for (retried
    until NumberHub's cancel lock opens). Each kind has its own cancel: a mailbox
    was asked about as if it were a number until 2026-11-25, which answered "not
    found" and left it open. A rented number goes back only inside its refund time."""
    now = time.monotonic()
    for nh_id, (rid, due, kind) in list(_orphans.items()):
        if due > now:
            continue
        cli = client_for(rid)
        if cli is None:
            continue
        try:
            body = await (cli.cancel_rental(nh_id) if kind == "rent" else
                          cli.cancel_email(nh_id) if kind == "email" else cli.cancel(nh_id))
        except NumberHubError as exc:
            if exc.code == "cancel_locked":
                _orphans[nh_id] = (rid, now + int(exc.data.get("seconds_remaining") or 15) + 3, kind)
            elif exc.code == "cancel_window_closed" and exc.data.get("window_state") == "locked":
                _orphans[nh_id] = (rid, now + 40, kind)          # a rental's refund opens after two minutes
            elif exc.status == 404 or exc.code == "cancel_window_closed":
                _orphans.pop(nh_id, None)
                if kind == "rent" and exc.status != 404:
                    await _rent_not_returned(rid, nh_id)
            else:
                _orphans[nh_id] = (rid, now + 30, kind)
            continue
        _orphans.pop(nh_id, None)
        if kind == "rent" and not body.get("ok"):
            await _rent_not_returned(rid, nh_id)
            continue
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
        try:
            await _recover_one(order)
        except Exception:  # noqa: BLE001
            # One order that cannot be handled stopped the recovery of every shop's.
            log.exception("order %s: recovery crashed; the others go on", order.id)


async def _recover_one(order: Order) -> None:
    for order in (order,):              # one pass: every `continue` below ends this order's turn
        if order.id in _inflight:
            continue                    # a live buy() is still on it
        if _age_sec(order) > BUYING_GIVE_UP_SEC:
            # Past NumberHub's key retention a replay would be a NEW purchase.
            # Any number from back then has long expired: give the hold back.
            if await repo.fail_order(order.id, "no_answer"):
                log.warning("order %s: no answer from NumberHub for 20 h; member hold released", order.id)
                if order.kind in ("proxy", "rent", "esim"):
                    # Both are paid when bought and last for days: if the purchase
                    # happened after all, the owner has it and paid for it. Say so.
                    shop = await repo.get_reseller(order.reseller_id)
                    if shop is not None:
                        asyncio.ensure_future(warn_owner(shop, order.kind + "_unconfirmed"))
            continue
        reseller = await repo.get_reseller(order.reseller_id)
        cli = client_for(order.reseller_id)
        if reseller is None or cli is None:
            continue
        # The replay needs the SAME request body: the ceiling stored at purchase.
        # Orders from before it was stored get it back from the member price
        # (ceil(c*k)/k floored to cents == c), true while prices were commission-only.
        ceiling = (Decimal(order.ceiling_at_buy) if order.ceiling_at_buy is not None
                   else original_ceiling(order.member_price, order.markup_pct_at_buy))
        _inflight.add(order.id)
        try:
            if order.kind == "email":
                nh = await cli.buy_email(order.service, order.country, ceiling, idempotency_key(order))
            elif order.kind == "proxy":
                nh = await cli.buy_proxy(int(order.country), ceiling, idempotency_key(order))
            elif order.kind == "rent":
                nh = await cli.buy_rental(int(order.rent_hours or 0), order.country, order.service, ceiling,
                                          idempotency_key(order))
            elif order.kind == "esim":
                nh = await cli.buy_esim(order.country, order.service, ceiling, idempotency_key(order))
            else:
                nh = await cli.buy(order.service, order.country, ceiling, idempotency_key(order))
        except TransportError:
            continue                    # still unclear: try again next sweep
        except NumberHubError as exc:
            # Only the purchase handler's own answer proves there is no order.
            # 429, 401/403 and the idempotency gate say nothing about it.
            if authoritative(exc):
                await repo.fail_order(order.id, exc.code)
            continue
        finally:
            _inflight.discard(order.id)
        if await _attach(reseller, order.id, nh):
            log.info("order %s: recovered purchase (nh %s)", order.id, nh.get("id"))
            if order.kind in ("proxy", "esim"):
                await settle_order(await repo.get_order(order.id))     # paid when bought (an eSIM: once delivered)
            await _show_recovered(reseller, order.id)


async def _show_recovered(reseller: Reseller, order_id: int) -> None:
    """The member was told "processing": send them the number now."""
    from app import runtime, texts
    bot = runtime.bot_for(reseller.id)
    order = await repo.get_order(order_id)
    member = await repo.get_member(order.member_id) if order else None
    if bot is None or member is None:
        return
    if order.kind == "esim" and order.status in Order.CHARGED_STATUSES:
        # Delivered with the recovered purchase: its QR and card, as a fresh purchase gets.
        if await send_esim(bot, reseller, member, order):
            await repo.set_codes_announced(order.id, 1)
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
    if order.kind in ("rent", "proxy"):
        return False, "closed", 0       # a rented number has its own way back (cancel_rental); a package has none
    if order.status not in ("pending", "waiting"):
        return False, "closed", 0
    cli = client_for(reseller.id)
    if cli is None:
        return False, "busy", 0
    email = order.kind == "email"
    try:
        body = await (cli.cancel_email(order.nh_id) if email else cli.cancel(order.nh_id))
    except NumberHubError as exc:
        if exc.code == "cancel_locked":
            return False, "locked", int(exc.data.get("seconds_remaining") or 15)
        if exc.code == "not_cancelable":
            return False, "closed", 0
        if exc.status == 404:
            return False, "not_found", 0
        return False, "busy", 0
    if body.get("email" if email else "number"):
        await repo.apply_nh_state(order.id, body["email" if email else "number"])
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


async def settle_order(order: Order | None, cancelling: bool = False) -> bool:
    if order is not None and getattr(order, "kind", "sms") == "rent":
        if order.settled != Order.UNSETTLED:
            return False
        # While a cancel is being asked or confirmed, nothing else decides about the money.
        if not cancelling and (order.id in _rent_cancelling or order.id in _rent_cancel_seen):
            return False
        due = rental_due(order)
        if due is None:
            return False
        done = await repo.settle(order.id, due, open_ok=True)
        if done:
            log.info("rental %s settled: member %s %s", order.id, "charged" if due else "refunded",
                     order.member_price)
        return done
    if order is None or order.status in Order.OPEN or order.settled != Order.UNSETTLED:
        return False
    charge = order.status in Order.CHARGED_STATUSES or bool(order_codes(order))
    done = await repo.settle(order.id, charge)
    if done:
        log.info("order %s settled: member %s %s", order.id, "charged" if charge else "refunded",
                 order.member_price)
    return done


_last_card_edit: dict[int, float] = {}
_mail_tries: dict[int, int] = {}         # email order -> times its mail could not be read yet
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
    live = (await repo.open_orders(reseller.id) + await repo.recently_received(reseller.id)
            + await repo.esim_unannounced(reseller.id))
    live = list({o.id: o for o in live}.values())
    if not live:
        _track(reseller.id, set())
        return
    cli = client_for(reseller.id)
    if cli is None:
        return
    # Nothing live but rented numbers past their refund time: the only news there is an
    # SMS now and then, for days. Asked every RENT_SYNC_SEC, not on every pass.
    if all(o.kind == "rent" and o.settled != Order.UNSETTLED for o in live):
        if now - _rent_synced.get(reseller.id, float("-inf")) < RENT_SYNC_SEC:
            return
    _rent_synced[reseller.id] = now
    try:
        listed = {int(o["id"]): o for o in await cli.orders(limit=100) if o.get("id") is not None}
    except NumberHubError as exc:
        await _sync_trouble(reseller, exc, cli)
        return
    from app import runtime, texts
    bot = runtime.bot_for(reseller.id)
    missing = sorted((o for o in live if o.nh_id not in listed
                      and not (o.kind == "rent" and now - _offlist_polled.get(o.id, float("-inf")) < RENT_SYNC_SEC)),
                     key=lambda o: _offlist_polled.get(o.id, 0))
    polled = {o.id for o in missing[:OFFLIST_PER_PASS]}
    for order in live:
        nh = listed.get(order.nh_id)
        if nh is None:
            if order.id not in polled:
                continue
            _offlist_polled[order.id] = now
            try:
                nh = await (cli.email(order.nh_id) if order.kind == "email" else
                            cli.rental(order.nh_id) if order.kind == "rent" else
                            cli.esim(order.nh_id) if order.kind == "esim" else cli.number(order.nh_id))
            except NumberHubError as exc:
                if exc.status in (401, 403, 429):
                    await _sync_trouble(reseller, exc, cli)
                    return
                continue
        if order.kind == "rent":
            if order.id in _rent_cancelling:
                continue                # the customer's own cancel is waiting for NumberHub's answer
            if str(nh.get("status") or "") == "canceled" and order.status != "canceled":
                # NumberHub marks a rental cancelled BEFORE it asks the number's source, and
                # puts it back when the source refuses. Believed when it is still cancelled
                # a minute later: a refund for a rental that kept running is the owner's loss.
                if now - _rent_cancel_seen.setdefault(order.id, now) < RENT_CANCEL_CONFIRM_SEC:
                    continue
            _rent_cancel_seen.pop(order.id, None)
        before = order.status
        changed, n_codes = await repo.apply_nh_state(order.id, nh)
        fresh = await repo.get_order(order.id)
        if fresh is None:
            continue
        settled_now = await settle_order(fresh)
        fresh = await repo.get_order(order.id)
        member = await repo.get_member(fresh.member_id)
        if bot is None or member is None:
            continue
        codes = order_codes(fresh)
        new_codes = n_codes > fresh.codes_announced
        if fresh.kind == "rent":
            # Every SMS goes to the customer with its text. A rental is counted by its
            # SMS (codes_announced), not by the codes in them: many carry none.
            sms = order_messages(fresh)
            announced = fresh.codes_announced
            for one in sms[announced:]:
                if await send_ex(bot, member.telegram_id, texts.rental_sms(member, fresh, one)) == "retry":
                    break               # Telegram is busy: the rest goes out next pass
                announced += 1
            new_codes = announced > fresh.codes_announced
            if new_codes:
                await repo.set_codes_announced(fresh.id, announced)
            if changed and fresh.status == "canceled":
                await send(bot, member.telegram_id, texts.rental_refunded(member, fresh))
            elif changed and fresh.status in ("completed", "expired"):
                await send(bot, member.telegram_id, texts.rental_ended(member, fresh))
        elif fresh.kind == "esim":
            # Delivered: its QR and its card go out once. The purchase sends an eSIM that
            # came with it and marks it; here are the ones that were ready later, or
            # whose message failed (asked again on the next pass).
            new_codes = False
            if fresh.status in Order.CHARGED_STATUSES:
                if (fresh.codes_announced == 0 and _age_sec(fresh) > ESIM_PUSH_AFTER_SEC
                        and await send_esim(bot, reseller, member, fresh)):
                    await repo.set_codes_announced(fresh.id, 1)
                continue                # a delivered eSIM's card is the full one: the sync never redraws it
            if changed and fresh.status in ("canceled", "expired"):
                await send(bot, member.telegram_id, texts.refunded(member, fresh))
        elif fresh.kind == "email":
            # One mail per mailbox, and it may carry a link and no code at all: tell
            # the customer once, when the order shows it received its mail.
            new_codes = False
            if fresh.codes_announced == 0 and (codes or fresh.status in Order.CHARGED_STATUSES):
                mail = await mail_details(reseller, fresh)
                if not mail and not codes and _mail_tries.get(fresh.id, 0) < 5:
                    # The mail could not be read this time. Announced now, a mail with a
                    # link and no code would be charged and never shown: ask again.
                    _mail_tries[fresh.id] = _mail_tries.get(fresh.id, 0) + 1
                elif await send_ex(bot, member.telegram_id, texts.mail_arrived(member, fresh, mail, codes)) != "retry":
                    _mail_tries.pop(fresh.id, None)
                    await repo.set_codes_announced(fresh.id, max(1, len(codes)))
                    new_codes = True
            elif changed and fresh.status in ("canceled", "expired") and not codes:
                await send(bot, member.telegram_id, texts.refunded(member, fresh))
        elif new_codes:
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
        if (fresh.kind == "rent" and fresh.settled != Order.UNSETTLED) or fresh.kind == "esim":
            due = False                 # nothing counts down on these: redrawn only when something changes
        if changed or new_codes or due or settled_now:
            await refresh_card(bot, member, fresh)
    _track(reseller.id, {o.id for o in live})


_order_reseller: dict[int, int] = {}


def _track(reseller_id: int, live_ids: set[int]) -> None:
    """Forget the per-order bookkeeping of orders that left the live set."""
    for oid in [k for k, rid in _order_reseller.items() if rid == reseller_id and k not in live_ids]:
        _order_reseller.pop(oid, None)
        _last_card_edit.pop(oid, None)
        _offlist_polled.pop(oid, None)
        _rent_cancel_seen.pop(oid, None)
    for oid in live_ids:
        _order_reseller[oid] = reseller_id


async def _sync_trouble(reseller: Reseller, exc: NumberHubError, cli: NumberHub) -> None:
    """The key can't read orders right now: back off instead of hammering."""
    now = time.monotonic()
    if exc.status == 429:
        _backoff_until[reseller.id] = now + 15
    elif exc.status in (401, 403):
        _backoff_until[reseller.id] = now + 60
        problem = _key_problem(exc)
        if exc.status == 401:
            await key_rejected(reseller.id, cli)
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
    # Rented numbers are settled while they run, by the order sync. Here: the ones a
    # paused shop's sync is not looking at, from what NumberHub last said about them.
    for order in await repo.unsettled_rentals():
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
