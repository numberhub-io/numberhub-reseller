"""Data access. A member's balance only ever changes through one conditional
UPDATE (never read-modify-write), rounded to cents inside SQL, written in the
same transaction as its audit row."""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.db import session_factory
from app.models import Member, MemberTx, Order, Reseller


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def money2(expr):  # noqa: ANN001
    # SQLite stores NUMERIC as REAL: round in SQL so the money gates compare cents.
    return func.round(expr, 2)


def _parse_ts(value) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# ─── resellers ───────────────────────────────────────────────────────────────
async def create_reseller(**values) -> Reseller:
    async with session_factory() as s:
        row = Reseller(**values)
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row


async def get_reseller(reseller_id: int) -> Reseller | None:
    async with session_factory() as s:
        return await s.get(Reseller, reseller_id)


async def get_reseller_by_bot_id(bot_id: int) -> Reseller | None:
    async with session_factory() as s:
        return (await s.execute(select(Reseller).where(Reseller.bot_id == bot_id))).scalar_one_or_none()


async def list_resellers(status: str | None = None, owner_id: int | None = None) -> list[Reseller]:
    async with session_factory() as s:
        q = select(Reseller).order_by(Reseller.id)
        if status:
            q = q.where(Reseller.status == status)
        if owner_id is not None:
            q = q.where(Reseller.owner_id == owner_id)
        return list((await s.execute(q)).scalars())


async def update_reseller(reseller_id: int, **values) -> None:
    allowed = {"markup_pct", "welcome_text", "support_contact", "status", "bot_username", "bot_title",
               "bot_token_enc", "api_key_enc", "api_key_hint"}
    values = {k: v for k, v in values.items() if k in allowed}
    if values:
        async with session_factory() as s:
            await s.execute(update(Reseller).where(Reseller.id == reseller_id).values(**values))
            await s.commit()


# ─── members ─────────────────────────────────────────────────────────────────
async def get_or_create_member(reseller_id: int, telegram_id: int, username: str | None,
                               full_name: str | None, language: str = "en") -> Member:
    async with session_factory() as s:
        q = select(Member).where(Member.reseller_id == reseller_id, Member.telegram_id == telegram_id)
        row = (await s.execute(q)).scalar_one_or_none()
        if row is not None:
            row.username, row.full_name, row.last_seen_at = username, full_name, _now()
            await s.commit()
            return row
        row = Member(reseller_id=reseller_id, telegram_id=telegram_id, username=username,
                     full_name=full_name, language=language)
        s.add(row)
        try:
            await s.commit()
        except IntegrityError:            # the member's first two updates raced
            await s.rollback()
            return (await s.execute(q)).scalar_one()
        await s.refresh(row)
        return row


async def get_member(member_id: int) -> Member | None:
    async with session_factory() as s:
        return await s.get(Member, member_id)


class AmbiguousMember(Exception):
    """More than one customer of this shop has used that @username."""


async def find_member(reseller_id: int, ref: str) -> Member | None:
    """By the ID the bot shows a member (their Telegram id) or by @username.
    Usernames can be changed and reused, so a name two customers have carried
    raises AmbiguousMember instead of picking one of them."""
    ref = (ref or "").strip()
    async with session_factory() as s:
        if ref.lstrip("-").isdigit():
            if len(ref) > 19:
                return None
            q = select(Member).where(Member.reseller_id == reseller_id, Member.telegram_id == int(ref))
            return (await s.execute(q)).scalars().first()
        name = ref.lstrip("@").lower()
        if not name:
            return None
        q = select(Member).where(Member.reseller_id == reseller_id, func.lower(Member.username) == name).limit(2)
        rows = list((await s.execute(q)).scalars())
        if len(rows) > 1:
            raise AmbiguousMember(name)
        return rows[0] if rows else None


async def list_members(reseller_id: int, limit: int = 20) -> list[Member]:
    async with session_factory() as s:
        q = (select(Member).where(Member.reseller_id == reseller_id)
             .order_by(Member.last_seen_at.desc()).limit(limit))
        return list((await s.execute(q)).scalars())


async def member_chat_ids(reseller_id: int) -> list[int]:
    async with session_factory() as s:
        q = select(Member.telegram_id).where(Member.reseller_id == reseller_id, Member.is_blocked.is_(False))
        return list((await s.execute(q)).scalars())


async def set_member_language(member_id: int, language: str) -> None:
    async with session_factory() as s:
        await s.execute(update(Member).where(Member.id == member_id).values(language=language))
        await s.commit()


async def set_member_last(member_id: int, service: str, country: str) -> None:
    async with session_factory() as s:
        await s.execute(update(Member).where(Member.id == member_id)
                        .values(last_service=service, last_country=str(country)))
        await s.commit()


async def set_member_blocked(member_id: int, blocked: bool) -> None:
    async with session_factory() as s:
        await s.execute(update(Member).where(Member.id == member_id).values(is_blocked=blocked))
        await s.commit()


async def member_adjust(reseller_id: int, member_id: int, amount: Decimal) -> bool:
    """The reseller adds (+) or removes (-) credit. A removal can't touch credit
    reserved by an open order, nor go below zero."""
    if not amount.is_finite() or amount == 0:
        return False
    async with session_factory() as s:
        cond = [Member.id == member_id, Member.reseller_id == reseller_id]
        if amount < 0:
            cond.append(money2(Member.balance - Member.held) >= -amount)
        res = await s.execute(update(Member).where(*cond).values(balance=money2(Member.balance + amount)))
        if res.rowcount != 1:
            await s.rollback()
            return False
        s.add(MemberTx(reseller_id=reseller_id, member_id=member_id,
                       kind="credit" if amount > 0 else "debit", amount=amount))
        await s.commit()
        return True


def _hold_stmt(member_id: int, amount: Decimal):
    # Rounded on both sides: SQLite keeps balances as REAL, and 0.30 - 0.10 is
    # 0.19999999999999998 there, which refused a member with exactly the price.
    return update(Member).where(
        Member.id == member_id, Member.is_blocked.is_(False), money2(Member.balance - Member.held) >= amount,
    ).values(held=money2(Member.held + amount))


async def member_try_hold(member_id: int, amount: Decimal) -> bool:
    if not amount.is_finite() or amount <= 0:
        raise ValueError("hold must be positive")
    async with session_factory() as s:
        res = await s.execute(_hold_stmt(member_id, amount))
        await s.commit()
        return res.rowcount == 1


# ─── orders ──────────────────────────────────────────────────────────────────
async def create_order(**values) -> Order:
    async with session_factory() as s:
        row = Order(status=Order.BUYING, settled=Order.UNSETTLED, **values)
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row


async def create_order_with_hold(**values) -> Order | None:
    """Hold the member's price and create the BUYING order in ONE transaction, so
    a crash or an error in between can never strand a hold without an order.
    None = not enough credit (nothing changed)."""
    price = Decimal(values["member_price"])
    if not price.is_finite() or price <= 0:
        raise ValueError("hold must be positive")
    async with session_factory() as s:
        res = await s.execute(_hold_stmt(values["member_id"], price))
        if res.rowcount != 1:
            await s.rollback()
            return None
        row = Order(status=Order.BUYING, settled=Order.UNSETTLED, **values)
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row


async def get_order(order_id: int) -> Order | None:
    async with session_factory() as s:
        return await s.get(Order, order_id)


def _nh_values(nh: dict) -> dict:
    return {
        "nh_id": int(nh["id"]),
        "status": str(nh.get("status") or "pending"),
        "phone": nh.get("phone") or None,
        "nh_price": Decimal(str(nh.get("price") or 0)),
        "codes": json.dumps([str(c) for c in (nh.get("codes") or []) if c], ensure_ascii=False),
        "expires_at": _parse_ts(nh.get("expires_at")),
        "cancel_available_at": _parse_ts(nh.get("cancel_available_at")),
        "service_name": nh.get("service_name") or None,
        "country_name": nh.get("country_name") or None,
        "updated_at": _now(),
    }


def _link_values(nh: dict) -> dict:
    # Our own tidy service and country names stay; NumberHub's raw catalog name
    # ("Instagram+Threads") would replace them on every card and message.
    vals = _nh_values(nh)
    vals.pop("service_name")
    vals.pop("country_name")
    return vals


async def link_order(order_id: int, nh: dict) -> bool:
    """The NumberHub order now exists: record it on our row, but ONLY while the
    row is still BUYING with its hold in place. A FAILED row has already given
    the hold back; linking it would let a code through that is never charged.
    False = not linked (the caller must revive it or give the number back)."""
    async with session_factory() as s:
        res = await s.execute(update(Order).where(
            Order.id == order_id, Order.status == Order.BUYING, Order.settled == Order.UNSETTLED,
        ).values(**_link_values(nh)))
        await s.commit()
        return res.rowcount == 1


async def revive_failed(order_id: int, nh: dict) -> bool:
    """A purchase we had given up on turned out to exist. Take the member's hold
    again and reopen the order, in one transaction; False when the member no
    longer has the credit (then the number must be given back at NumberHub)."""
    async with session_factory() as s:
        o = await s.get(Order, order_id)
        if o is None or o.status != Order.FAILED or o.settled != Order.RELEASED:
            return False
        hold = await s.execute(_hold_stmt(o.member_id, o.member_price))
        if hold.rowcount != 1:
            await s.rollback()
            return False
        res = await s.execute(update(Order).where(
            Order.id == order_id, Order.status == Order.FAILED, Order.settled == Order.RELEASED,
        ).values(settled=Order.UNSETTLED, **_link_values(nh)))
        if res.rowcount != 1:
            await s.rollback()
            return False
        await s.commit()
        return True


_RANK = {"buying": 0, "pending": 1, "waiting": 2}


def _rank(status: str) -> int:
    if status in Order.TERMINAL:
        return 4
    return _RANK.get(status, 3)            # received and NumberHub's transient ones


async def apply_nh_state(order_id: int, nh: dict) -> tuple[bool, int]:
    """Mirror NumberHub's view of the order. Returns (status_changed, codes_now).

    Forward only: a list fetched before a cancel can't reopen the cancelled order
    (the member got a wrong "no code" message), an ended order never changes
    status again, and the stored codes never shrink."""
    vals = _nh_values(nh)
    vals.pop("nh_id")
    for k in ("service_name", "country_name"):
        vals.pop(k)
    async with session_factory() as s:
        cur = await s.get(Order, order_id)
        if cur is None or cur.status in (Order.BUYING, Order.FAILED):
            return False, 0
        have = [str(c) for c in json.loads(cur.codes or "[]") if c]
        got = json.loads(vals["codes"])
        if len(got) < len(have):
            vals["codes"] = json.dumps(have, ensure_ascii=False)
        new = vals["status"]
        if cur.status in Order.TERMINAL or _rank(new) < _rank(cur.status):
            vals["status"] = cur.status
        changed = vals["status"] != cur.status
        # Only over the state we read: a cancel that landed meanwhile wins.
        res = await s.execute(update(Order).where(Order.id == order_id, Order.status == cur.status)
                              .values(**vals))
        await s.commit()
        if res.rowcount != 1:
            return False, len(have)
        return changed, len(json.loads(vals["codes"]))


async def set_codes_announced(order_id: int, n: int) -> None:
    async with session_factory() as s:
        await s.execute(update(Order).where(Order.id == order_id, Order.codes_announced < n)
                        .values(codes_announced=n))
        await s.commit()


async def set_card(order_id: int, chat_id: int, message_id: int) -> None:
    """This message now shows this order. Any other order that was drawn on the
    same message (📱 New number on a delivered card, a double tap) lets go of
    it, or the sync would keep redrawing the old order over the new one."""
    async with session_factory() as s:
        await s.execute(update(Order).where(Order.chat_id == chat_id, Order.message_id == message_id,
                                            Order.id != order_id).values(chat_id=None, message_id=None))
        await s.execute(update(Order).where(Order.id == order_id).values(chat_id=chat_id, message_id=message_id))
        await s.commit()


async def fail_order(order_id: int) -> bool:
    """The purchase never happened: FAILED, and the member's hold is released."""
    async with session_factory() as s:
        claim = await s.execute(update(Order).where(Order.id == order_id, Order.status == Order.BUYING,
                                                    Order.settled == Order.UNSETTLED)
                                .values(status=Order.FAILED, settled=Order.RELEASED, updated_at=_now()))
        if claim.rowcount != 1:
            await s.rollback()
            return False
        o = await s.get(Order, order_id)
        res = await s.execute(update(Member).where(Member.id == o.member_id, Member.held >= o.member_price)
                              .values(held=money2(Member.held - o.member_price)))
        if res.rowcount != 1:
            await s.rollback()
            return False
        await s.commit()
        return True


async def settle(order_id: int, charge: bool) -> bool:
    """Close the member side of an ended order exactly once: charge the held price
    (a code arrived) or release it. Claim and member update commit together."""
    target = Order.CHARGED if charge else Order.RELEASED
    async with session_factory() as s:
        claim = await s.execute(update(Order).where(Order.id == order_id, Order.settled == Order.UNSETTLED,
                                                    Order.status.not_in(Order.OPEN))
                                .values(settled=target))
        if claim.rowcount != 1:
            await s.rollback()
            return False
        o = await s.get(Order, order_id)
        price = o.member_price
        if charge:
            res = await s.execute(update(Member).where(
                Member.id == o.member_id, Member.held >= price, Member.balance >= price,
            ).values(balance=money2(Member.balance - price), held=money2(Member.held - price)))
            if res.rowcount == 1:
                s.add(MemberTx(reseller_id=o.reseller_id, member_id=o.member_id, kind="charge",
                               amount=-price, order_id=o.id))
        else:
            res = await s.execute(update(Member).where(Member.id == o.member_id, Member.held >= price)
                                  .values(held=money2(Member.held - price)))
        if res.rowcount != 1:
            await s.rollback()         # the hold is not there: leave it unsettled and visible
            return False
        await s.commit()
        return True


async def open_orders(reseller_id: int) -> list[Order]:
    async with session_factory() as s:
        q = select(Order).where(Order.reseller_id == reseller_id, Order.status.in_(("pending", "waiting")),
                                Order.nh_id.is_not(None))
        return list((await s.execute(q)).scalars())


async def recently_received(reseller_id: int, minutes: int = 25) -> list[Order]:
    """Delivered orders still inside their window (more codes can arrive), and
    any status NumberHub may report that we don't know as final (requesting,
    reactivating): those are polled rather than frozen. The window is the order's
    own expiry plus 5 minutes (updated_at moves on every poll, so it can't be the
    clock)."""
    async with session_factory() as s:
        now = _now()
        live = or_(Order.expires_at >= now - dt.timedelta(minutes=5),
                   and_(Order.expires_at.is_(None), Order.created_at >= now - dt.timedelta(minutes=minutes)))
        q = select(Order).where(Order.reseller_id == reseller_id,
                                Order.status.not_in(Order.OPEN + Order.TERMINAL),
                                live, Order.nh_id.is_not(None))
        return list((await s.execute(q)).scalars())


async def unsettled_ended(limit: int = 200) -> list[Order]:
    async with session_factory() as s:
        q = select(Order).where(Order.settled == Order.UNSETTLED, Order.status.not_in(Order.OPEN)).limit(limit)
        return list((await s.execute(q)).scalars())


async def stale_buying(older_than_sec: int = 300) -> list[Order]:
    """BUYING rows whose purchase call never finished (crash mid-request)."""
    async with session_factory() as s:
        cutoff = _now() - dt.timedelta(seconds=older_than_sec)
        q = select(Order).where(Order.status == Order.BUYING, Order.created_at < cutoff)
        return list((await s.execute(q)).scalars())


async def member_orders(member_id: int, limit: int = 10) -> list[Order]:
    async with session_factory() as s:
        q = (select(Order).where(Order.member_id == member_id, Order.status != Order.FAILED)
             .order_by(Order.created_at.desc()).limit(limit))
        return list((await s.execute(q)).scalars())


async def member_open_count(member_id: int, service: str | None = None, country: str | None = None) -> int:
    async with session_factory() as s:
        q = select(func.count()).select_from(Order).where(Order.member_id == member_id,
                                                          Order.status.in_(Order.OPEN))
        if service is not None:
            q = q.where(Order.service == service, Order.country == str(country))
        return (await s.execute(q)).scalar_one()


async def stats(reseller_id: int, days: int) -> dict:
    """Delivered orders in the window: members paid `sales`, NumberHub held
    `cost` at most (its final charge can only be lower)."""
    async with session_factory() as s:
        cutoff = _now() - dt.timedelta(days=days)
        n, sales, cost = (await s.execute(
            select(func.count(), func.coalesce(func.sum(Order.member_price), 0),
                   func.coalesce(func.sum(Order.nh_price), 0))
            .where(Order.reseller_id == reseller_id, Order.settled == Order.CHARGED,
                   Order.created_at >= cutoff))).one()
        members = (await s.execute(select(func.count()).select_from(Member)
                                   .where(Member.reseller_id == reseller_id))).scalar_one()
        return {"orders": int(n), "sales": Decimal(str(sales or 0)).quantize(Decimal("0.01")),
                "cost": Decimal(str(cost or 0)).quantize(Decimal("0.01")), "members": int(members)}
