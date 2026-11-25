"""Data access. A member's balance only ever changes through one conditional
UPDATE (never read-modify-write), rounded to cents inside SQL, written in the
same transaction as its audit row."""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

from sqlalchemy import and_, delete, func, or_, select, update, case
from sqlalchemy.exc import IntegrityError

from app.db import session_factory
from app.models import Deposit, Member, MemberTx, Order, Payment, PriceRule, Reseller


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
               "bot_token_enc", "api_key_enc", "api_key_hint", "max_profit", "deposit_info", "deposit_min",
               "oxapay_key_enc", "cryptobot_token_enc", "nh_pay", "daily_report",
               "kasera_key_enc", "kasera_rate", "key_by_owner", "email_on", "kasera_auto", "kasera_extra",
               "pay_note", "proxy_on", "rent_on", "esim_on", "suspended_from"}
    values = {k: v for k, v in values.items() if k in allowed}
    if values:
        async with session_factory() as s:
            await s.execute(update(Reseller).where(Reseller.id == reseller_id).values(**values))
            await s.commit()


async def mark_key_invalid(reseller_id: int, api_key_enc: str) -> bool:
    """ACTIVE -> KEY_INVALID, only while the shop still stores the key NumberHub
    rejected: a reconnect that saved a new key in between is never undone."""
    async with session_factory() as s:
        res = await s.execute(update(Reseller).where(
            Reseller.id == reseller_id, Reseller.status == Reseller.ACTIVE,
            Reseller.api_key_enc == api_key_enc).values(status=Reseller.KEY_INVALID))
        await s.commit()
        return res.rowcount == 1


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
    reserved by an open order, nor go below zero, nor take what the customer paid
    in with built-in payments: that is the customer's money and can only be spent."""
    if not amount.is_finite() or amount == 0:
        return False
    async with session_factory() as s:
        cond = [Member.id == member_id, Member.reseller_id == reseller_id]
        if amount < 0:
            cond.append(money2(Member.balance - Member.held) >= -amount)
            cond.append(money2(Member.balance - func.coalesce(Member.paid_left, 0)) >= -amount)
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


def _message(m: dict) -> dict:
    return {"from": str(m.get("from") or "")[:64], "text": str(m.get("text") or "")[:1200],
            "code": str(m.get("code") or "")[:32]}


def _load_list(raw) -> list:
    try:
        rows = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return rows if isinstance(rows, list) else []


def _nh_values(nh: dict) -> dict:
    vals = {
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
    if str(nh.get("kind") or "") == "rent":
        # A rented number: every SMS with its text (`codes` has only the codes read out
        # of them), and from when until when a cancel gives the money back. NumberHub
        # sends both times empty once it no longer offers a refund.
        if isinstance(nh.get("messages"), list):
            vals["messages"] = json.dumps([_message(m) for m in nh["messages"] if isinstance(m, dict)][:300],
                                          ensure_ascii=False)
        vals["cancel_available_at"] = _parse_ts(nh.get("refund_from"))
        vals["refund_until"] = _parse_ts(nh.get("refund_until"))
    return vals


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
        count, had = len(json.loads(vals["codes"])), len(have)
        if cur.kind == "rent":
            # A rental is counted by its SMS, not by the codes in them; they never shrink.
            kept = _load_list(cur.messages)
            if "messages" not in vals or len(_load_list(vals["messages"])) < len(kept):
                vals["messages"] = json.dumps(kept, ensure_ascii=False)
            count, had = len(_load_list(vals["messages"])), len(kept)
        else:
            vals.pop("messages", None)
            vals.pop("refund_until", None)
        new = vals["status"]
        if cur.status in Order.TERMINAL or _rank(new) < _rank(cur.status):
            vals["status"] = cur.status
        changed = vals["status"] != cur.status
        # Only over the state we read: a cancel that landed meanwhile wins.
        res = await s.execute(update(Order).where(Order.id == order_id, Order.status == cur.status)
                              .values(**vals))
        await s.commit()
        if res.rowcount != 1:
            return False, had
        return changed, count


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


async def fail_order(order_id: int, reason: str | None = None) -> bool:
    """The purchase never happened: FAILED, and the member's hold is released.
    `reason` is kept for the owner's statistics (why purchases fail)."""
    async with session_factory() as s:
        claim = await s.execute(update(Order).where(Order.id == order_id, Order.status == Order.BUYING,
                                                    Order.settled == Order.UNSETTLED)
                                .values(status=Order.FAILED, settled=Order.RELEASED, updated_at=_now(),
                                        fail_reason=(str(reason)[:32] if reason else None)))
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


async def settle(order_id: int, charge: bool, open_ok: bool = False) -> bool:
    """Close the member side of an ended order exactly once: charge the held price
    (a code arrived) or release it. Claim and member update commit together.
    `open_ok`: a rented number is charged while it is still running (it stays
    "waiting" for days), as soon as it can no longer be given back."""
    target = Order.CHARGED if charge else Order.RELEASED
    not_yet = (Order.BUYING,) if open_ok else Order.OPEN
    async with session_factory() as s:
        claim = await s.execute(update(Order).where(Order.id == order_id, Order.settled == Order.UNSETTLED,
                                                    Order.status.not_in(not_yet))
                                .values(settled=target))
        if claim.rowcount != 1:
            await s.rollback()
            return False
        o = await s.get(Order, order_id)
        price = o.member_price
        if charge:
            # Paid-in money is spent first: a customer who paid is never held up by a
            # bonus the owner added, and what the shop owes them falls with every code.
            left = func.coalesce(Member.paid_left, 0)
            res = await s.execute(update(Member).where(
                Member.id == o.member_id, Member.held >= price, Member.balance >= price,
            ).values(balance=money2(Member.balance - price), held=money2(Member.held - price),
                     paid_left=case((left > price, money2(left - price)), else_=0)))
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
        # A data package stays "received" for its whole 30 days and nothing arrives on
        # it: it is read when its owner opens it, never in this loop.
        q = select(Order).where(Order.reseller_id == reseller_id,
                                Order.status.not_in(Order.OPEN + Order.TERMINAL),
                                live, Order.nh_id.is_not(None), Order.kind != "proxy")
        return list((await s.execute(q)).scalars())


async def proxy_active_count(member_id: int) -> int:
    """Data packages this customer has that still work, or are being bought."""
    async with session_factory() as s:
        q = select(func.count()).select_from(Order).where(
            Order.member_id == member_id, Order.kind == "proxy", Order.status.in_((Order.BUYING, "received")),
            or_(Order.expires_at.is_(None), Order.expires_at > _now()))
        return int(await s.scalar(q) or 0)


async def esim_unannounced(reseller_id: int, minutes: int = 60) -> list[Order]:
    """eSIMs that were delivered in the last hour and whose QR has not gone out to the
    customer yet (the purchase sends it when the eSIM came with it; these came later,
    or that message failed)."""
    async with session_factory() as s:
        q = select(Order).where(Order.reseller_id == reseller_id, Order.kind == "esim",
                                Order.status.in_(Order.CHARGED_STATUSES), Order.codes_announced == 0,
                                Order.nh_id.is_not(None), Order.updated_at >= _now() - dt.timedelta(minutes=minutes))
        return list((await s.execute(q)).scalars())


async def rent_active_count(member_id: int) -> int:
    """Numbers this customer is renting now, or is renting at this very moment."""
    async with session_factory() as s:
        q = select(func.count()).select_from(Order).where(
            Order.member_id == member_id, Order.kind == "rent",
            Order.status.not_in(Order.TERMINAL), or_(Order.expires_at.is_(None), Order.expires_at > _now()))
        return int(await s.scalar(q) or 0)


async def unsettled_rentals(limit: int = 200) -> list[Order]:
    """Rented numbers whose price is still held: running ones and ended ones."""
    async with session_factory() as s:
        q = select(Order).where(Order.kind == "rent", Order.settled == Order.UNSETTLED,
                                Order.status.not_in((Order.BUYING, Order.FAILED))).limit(limit)
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


async def member_open_count(member_id: int, service: str | None = None, country: str | None = None,
                            kind: str | None = None) -> int:
    async with session_factory() as s:
        q = select(func.count()).select_from(Order).where(Order.member_id == member_id,
                                                          Order.status.in_(Order.OPEN))
        if service is not None:
            q = q.where(Order.service == service, Order.country == str(country))
        if kind is not None:
            q = q.where(Order.kind == kind)
        else:
            # A rented number stays "waiting" for its whole period. It is not an order
            # in progress: five rentals must not use up the open orders a customer may have.
            q = q.where(Order.kind != "rent")
        return (await s.execute(q)).scalar_one()


async def number_outcomes(member_id: int, hours: int = 24) -> tuple[int, int]:
    """(numbers of this customer that ended with no code, numbers that got one)
    among those bought in the last `hours`. Numbers still waiting count as neither."""
    since = _now() - dt.timedelta(hours=hours)
    async with session_factory() as s:
        rows = dict((await s.execute(
            select(Order.settled, func.count()).where(Order.member_id == member_id, Order.kind == "sms",
                                                      Order.created_at >= since, Order.status != Order.FAILED)
            .group_by(Order.settled))).all())
    return int(rows.get(Order.RELEASED, 0)), int(rows.get(Order.CHARGED, 0))


async def email_outcomes(member_id: int, hours: int = 24) -> tuple[int, int]:
    """(mailboxes of this customer that ended with no mail, mailboxes that got
    one) among those asked for in the last `hours`."""
    since = _now() - dt.timedelta(hours=hours)
    async with session_factory() as s:
        rows = dict((await s.execute(
            select(Order.settled, func.count()).where(Order.member_id == member_id, Order.kind == "email",
                                                      Order.created_at >= since, Order.status != Order.FAILED)
            .group_by(Order.settled))).all())
    return int(rows.get(Order.RELEASED, 0)), int(rows.get(Order.CHARGED, 0))


async def email_orders_since(minutes: int, member_id: int | None = None, reseller_id: int | None = None) -> int:
    """Mailboxes asked for in the last `minutes` by one customer or in one shop
    (a purchase that was refused opened none and does not count)."""
    since = _now() - dt.timedelta(minutes=minutes)
    async with session_factory() as s:
        q = select(func.count()).select_from(Order).where(Order.kind == "email", Order.created_at >= since,
                                                          Order.status != Order.FAILED)
        if member_id is not None:
            q = q.where(Order.member_id == member_id)
        if reseller_id is not None:
            q = q.where(Order.reseller_id == reseller_id)
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


async def shop_report(reseller_id: int, days: int) -> dict:
    """What the owner's statistics screen and morning report show for one shop."""
    since = _now() - dt.timedelta(days=days)
    dec = lambda v: Decimal(str(v or 0)).quantize(Decimal("0.01"))      # noqa: E731
    mine, recent = Order.reseller_id == reseller_id, Order.created_at >= since
    charged = Order.settled == Order.CHARGED
    async with session_factory() as s:
        outcomes = {st: int(n) for st, n in (await s.execute(
            select(Order.status, func.count()).where(mine, recent).group_by(Order.status))).all()}
        reasons = [(str(why or "unknown"), int(n)) for why, n in (await s.execute(
            select(Order.fail_reason, func.count()).where(mine, recent, Order.status == Order.FAILED)
            .group_by(Order.fail_reason).order_by(func.count().desc()))).all()]
        codes, sales, cost = (await s.execute(
            select(func.count(), func.coalesce(func.sum(Order.member_price), 0), func.coalesce(func.sum(Order.nh_price), 0))
            .where(mine, recent, charged))).one()
        apps = [(str(name or code), int(given or 0), int(got or 0), dec(sold), dec(sold) - dec(paid))
                for code, name, given, got, sold, paid in (await s.execute(
                    select(Order.service, func.max(Order.service_name),
                           func.sum(case((Order.nh_id.is_not(None), 1), else_=0)),
                           func.sum(case((charged, 1), else_=0)),
                           func.coalesce(func.sum(case((charged, Order.member_price), else_=0)), 0),
                           func.coalesce(func.sum(case((charged, Order.nh_price), else_=0)), 0))
                    .where(mine, recent).group_by(Order.service)
                    .order_by(func.sum(case((charged, Order.member_price), else_=0)).desc(), func.count().desc())
                    .limit(6))).all()]
        new_members = int(await s.scalar(select(func.count()).select_from(Member).where(
            Member.reseller_id == reseller_id, Member.created_at >= since)) or 0)
        seen = int(await s.scalar(select(func.count()).select_from(Member).where(
            Member.reseller_id == reseller_id, Member.last_seen_at >= since)) or 0)
        buyers = int(await s.scalar(select(func.count(func.distinct(Order.member_id))).where(mine, recent, charged)) or 0)
        members, balances = (await s.execute(select(func.count(), func.coalesce(func.sum(Member.balance), 0))
                                             .where(Member.reseller_id == reseller_id))).one()
        pays = [(str(prov), int(opened or 0), int(paid or 0), dec(total)) for prov, opened, paid, total in (await s.execute(
            select(Payment.provider, func.sum(case((Payment.invoice_id.is_not(None), 1), else_=0)),
                   func.sum(case((Payment.status == Payment.PAID, 1), else_=0)),
                   func.coalesce(func.sum(case((Payment.status == Payment.PAID, Payment.credited), else_=0)), 0))
            .where(Payment.reseller_id == reseller_id, Payment.created_at >= since).group_by(Payment.provider))).all()]
        dep_n, dep_sum = (await s.execute(
            select(func.count(), func.coalesce(func.sum(Deposit.credited), 0))
            .where(Deposit.reseller_id == reseller_id, Deposit.status == "approved", Deposit.created_at >= since))).one()
    return {"days": days, "outcomes": outcomes, "orders": sum(outcomes.values()), "reasons": reasons,
            "codes": int(codes), "sales": dec(sales), "profit": dec(sales) - dec(cost), "apps": apps,
            "new_members": new_members, "seen": seen, "buyers": buyers, "members": int(members),
            "balances": dec(balances), "payments": pays, "deposits": (int(dep_n), dec(dep_sum))}


async def claim_report_day(reseller_id: int, day: str) -> bool:
    """Win the right to send this shop's morning report for `day` (once)."""
    async with session_factory() as s:
        res = await s.execute(update(Reseller).where(
            Reseller.id == reseller_id, or_(Reseller.report_day.is_(None), Reseller.report_day != day))
            .values(report_day=day))
        await s.commit()
        return res.rowcount == 1


# ─── custom prices ───────────────────────────────────────────────────────────
async def price_rules(reseller_id: int) -> list[PriceRule]:
    async with session_factory() as s:
        q = select(PriceRule).where(PriceRule.reseller_id == reseller_id).order_by(PriceRule.service, PriceRule.country)
        return list((await s.execute(q)).scalars())


async def set_price_rule(reseller_id: int, service: str, country: str, mode: str, value: Decimal,
                         service_name: str | None = None, country_name: str | None = None) -> None:
    """Create or replace the rule for this app (+ country)."""
    async with session_factory() as s:
        row = (await s.execute(select(PriceRule).where(
            PriceRule.reseller_id == reseller_id, PriceRule.service == service,
            PriceRule.country == country))).scalar_one_or_none()
        if row is None:
            s.add(PriceRule(reseller_id=reseller_id, service=service, country=country, mode=mode, value=value,
                            service_name=service_name, country_name=country_name))
        else:
            row.mode, row.value = mode, value
            row.service_name, row.country_name = service_name or row.service_name, country_name or row.country_name
        await s.commit()


async def delete_price_rule(reseller_id: int, rule_id: int) -> bool:
    async with session_factory() as s:
        res = await s.execute(delete(PriceRule).where(PriceRule.id == rule_id, PriceRule.reseller_id == reseller_id))
        await s.commit()
        return res.rowcount == 1


async def count_price_rules(reseller_id: int) -> int:
    async with session_factory() as s:
        return int(await s.scalar(select(func.count(PriceRule.id)).where(PriceRule.reseller_id == reseller_id)) or 0)


# ─── deposits ────────────────────────────────────────────────────────────────
DEPOSITS_PER_DAY = 5


async def pending_deposit(member_id: int) -> Deposit | None:
    async with session_factory() as s:
        return (await s.execute(select(Deposit).where(Deposit.member_id == member_id,
                                                      Deposit.status == Deposit.PENDING)
                                .order_by(Deposit.id.desc()).limit(1))).scalar_one_or_none()


async def deposits_today(member_id: int) -> int:
    since = _now() - dt.timedelta(hours=24)
    async with session_factory() as s:
        return int(await s.scalar(select(func.count(Deposit.id)).where(
            Deposit.member_id == member_id, Deposit.created_at >= since)) or 0)


async def create_deposit(reseller_id: int, member_id: int, amount: Decimal, proof_text: str | None,
                         proof_photo: str | None) -> Deposit | None:
    """A new pending request, numbered #1, #2, ... per shop. None when the member
    already has one waiting (one at a time)."""
    for _ in range(5):                       # two shops' members racing for the same number
        async with session_factory() as s:
            if (await s.execute(select(Deposit.id).where(Deposit.member_id == member_id,
                                                         Deposit.status == Deposit.PENDING))).first():
                return None
            n = int(await s.scalar(select(func.max(Deposit.number)).where(Deposit.reseller_id == reseller_id)) or 0)
            row = Deposit(reseller_id=reseller_id, member_id=member_id, number=n + 1, amount=amount,
                          proof_text=proof_text, proof_photo=proof_photo, status=Deposit.PENDING)
            s.add(row)
            try:
                await s.commit()
            except Exception:  # noqa: BLE001 — the number was taken meanwhile: take the next
                await s.rollback()
                continue
            await s.refresh(row)
            return row
    return None


async def get_deposit(reseller_id: int, deposit_id: int) -> Deposit | None:
    async with session_factory() as s:
        row = await s.get(Deposit, deposit_id)
        return row if row is not None and row.reseller_id == reseller_id else None


async def pending_deposits(reseller_id: int, limit: int = 20) -> list[Deposit]:
    async with session_factory() as s:
        q = (select(Deposit).where(Deposit.reseller_id == reseller_id, Deposit.status == Deposit.PENDING)
             .order_by(Deposit.id).limit(limit))
        return list((await s.execute(q)).scalars())


async def decide_deposit(reseller_id: int, deposit_id: int, approve: bool,
                         amount: Decimal | None = None) -> Deposit | None:
    """Approve (credit) or reject a pending deposit. The status change and the
    credit happen in ONE transaction and only from pending, so a double tap, two
    devices or a retry credit the member exactly once. None = it was not pending."""
    async with session_factory() as s:
        row = await s.get(Deposit, deposit_id)
        if row is None or row.reseller_id != reseller_id:
            return None
        credit = Decimal(amount if amount is not None else row.amount) if approve else None
        if approve and (not credit.is_finite() or credit <= 0):
            return None
        res = await s.execute(update(Deposit).where(Deposit.id == deposit_id, Deposit.status == Deposit.PENDING)
                              .values(status=Deposit.APPROVED if approve else Deposit.REJECTED,
                                      credited=credit, decided_at=_now()))
        if res.rowcount != 1:
            await s.rollback()
            return None
        if approve:
            res = await s.execute(update(Member).where(Member.id == row.member_id, Member.reseller_id == reseller_id)
                                  .values(balance=money2(Member.balance + credit)))
            if res.rowcount != 1:
                await s.rollback()
                return None
            s.add(MemberTx(reseller_id=reseller_id, member_id=row.member_id, kind="deposit", amount=credit))
        await s.commit()
    return await get_deposit(reseller_id, deposit_id)



# ─── automatic payments (app/payments.py) ────────────────────────────────────
async def create_payment(reseller_id: int, member_id: int, provider: str, amount: Decimal) -> Payment:
    async with session_factory() as s:
        row = Payment(reseller_id=reseller_id, member_id=member_id, provider=provider, amount=amount,
                      status=Payment.PENDING)
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row


async def attach_invoice(payment_id: int, invoice_id: str, pay_url: str | None, expires_at: dt.datetime,
                         details: str | None = None) -> None:
    async with session_factory() as s:
        await s.execute(update(Payment).where(Payment.id == payment_id)
                        .values(invoice_id=invoice_id, pay_url=pay_url, expires_at=expires_at, details=details))
        await s.commit()


async def get_payment(payment_id: int) -> Payment | None:
    async with session_factory() as s:
        return await s.get(Payment, payment_id)


async def close_payment(payment_id: int, status: str) -> bool:
    """pending -> expired | failed | manual. False when it was not pending."""
    async with session_factory() as s:
        res = await s.execute(update(Payment).where(Payment.id == payment_id, Payment.status == Payment.PENDING)
                              .values(status=status))
        await s.commit()
        return res.rowcount == 1


async def credit_payment(payment_id: int, amount: Decimal | None = None, late: bool = False) -> Payment | None:
    """pending -> paid AND the member's balance + the amount, in ONE transaction.
    Only from pending, so the poller, the customer's check button and a restart
    racing each other credit it exactly once. None = it was not pending.

    `amount` is what really arrived when the gateway says so (a built-in payment
    is credited what NumberHub added to the shop's wallet); None = the asked amount.
    `late` also takes a payment that was given up (expired): it was paid after all."""
    async with session_factory() as s:
        row = await s.get(Payment, payment_id)
        if row is None:
            return None
        if row.provider == "numberhub" and (amount is None or Decimal(amount) <= 0):
            return None        # a built-in payment is never credited by its asked amount
        credit = (Decimal(amount).quantize(Decimal("0.01")) if amount is not None and Decimal(amount) > 0
                  else Decimal(row.amount))
        allowed = [Payment.PENDING, Payment.EXPIRED] if late else [Payment.PENDING]
        res = await s.execute(update(Payment).where(Payment.id == payment_id, Payment.status.in_(allowed))
                              .values(status=Payment.PAID, credited=credit, paid_at=_now()))
        if res.rowcount != 1:
            await s.rollback()
            return None
        values = {"balance": money2(Member.balance + credit)}
        if row.provider == "numberhub":
            # Paid into the shop's NumberHub wallet: the customer's own money.
            values["paid_left"] = money2(func.coalesce(Member.paid_left, 0) + credit)
        res = await s.execute(update(Member).where(Member.id == row.member_id, Member.reseller_id == row.reseller_id)
                              .values(**values))
        if res.rowcount != 1:
            await s.rollback()
            return None
        s.add(MemberTx(reseller_id=row.reseller_id, member_id=row.member_id, kind="payment", amount=credit))
        await s.commit()
    return await get_payment(payment_id)


async def open_payments(member_id: int) -> list[Payment]:
    """The member's unpaid links that can still be paid."""
    async with session_factory() as s:
        rows = list((await s.execute(select(Payment).where(
            Payment.member_id == member_id, Payment.status == Payment.PENDING,
            Payment.invoice_id.is_not(None)).order_by(Payment.id.desc()))).scalars())
    now = _now()
    return [p for p in rows if p.expires_at is None
            or (p.expires_at if p.expires_at.tzinfo else p.expires_at.replace(tzinfo=dt.timezone.utc)) > now]


async def payments_today(member_id: int) -> int:
    """Payments this customer opened in 24 h and did not pay. A refused attempt opened
    nothing and a paid one is a customer: ten paid $0.50 top-ups in a day, or a
    gateway that was down for ten taps, locked a customer out for a day."""
    since = _now() - dt.timedelta(hours=24)
    async with session_factory() as s:
        return int(await s.scalar(select(func.count(Payment.id)).where(
            Payment.member_id == member_id, Payment.created_at >= since,
            Payment.invoice_id.is_not(None), Payment.status != Payment.PAID)) or 0)


async def payments_today_shop(reseller_id: int, provider: str) -> int:
    """How many payment addresses a whole shop opened in 24 h that were not paid
    (the built-in payments' ceiling, payments.PER_DAY_PER_SHOP). A refused attempt
    opened nothing and a paid one is a customer, so neither counts."""
    since = _now() - dt.timedelta(hours=24)
    async with session_factory() as s:
        return int(await s.scalar(select(func.count(Payment.id)).where(
            Payment.reseller_id == reseller_id, Payment.provider == provider,
            Payment.invoice_id.is_not(None), Payment.status != Payment.PAID,
            Payment.created_at >= since)) or 0)


async def expired_builtin_payments(provider: str, days: int) -> list[Payment]:
    """Payments of one kind that were given up in the last `days` (payments.sweep_expired)."""
    since = _now() - dt.timedelta(days=days)
    async with session_factory() as s:
        return list((await s.execute(select(Payment).where(
            Payment.provider == provider, Payment.status == Payment.EXPIRED,
            Payment.invoice_id.is_not(None), Payment.created_at >= since)
            .order_by(Payment.id.desc()).limit(2000))).scalars())


async def paid_builtin_payments(provider: str, days: int) -> list[Payment]:
    """Payments of one kind that were credited in the last `days` (payments.check_more)."""
    since = _now() - dt.timedelta(days=days)
    async with session_factory() as s:
        return list((await s.execute(select(Payment).where(
            Payment.provider == provider, Payment.status == Payment.PAID,
            Payment.invoice_id.is_not(None), Payment.paid_at >= since)
            .order_by(Payment.id.desc()).limit(2000))).scalars())


async def credit_payment_more(payment_id: int, have: Decimal, now: Decimal) -> Decimal | None:
    """A credited payment received more: raise its credited amount from `have` to
    `now` AND add the difference to the member's balance, in ONE transaction. Only
    while the payment still shows `have`, so it can be added once. Returns the
    difference, or None."""
    extra = (Decimal(now) - Decimal(have)).quantize(Decimal("0.01"))
    if extra <= 0:
        return None
    async with session_factory() as s:
        row = await s.get(Payment, payment_id)
        if row is None:
            return None
        res = await s.execute(update(Payment).where(Payment.id == payment_id, Payment.status == Payment.PAID,
                                                    Payment.credited == have).values(credited=now))
        if res.rowcount != 1:
            await s.rollback()
            return None
        values = {"balance": money2(Member.balance + extra)}
        if row.provider == "numberhub":
            values["paid_left"] = money2(func.coalesce(Member.paid_left, 0) + extra)
        res = await s.execute(update(Member).where(Member.id == row.member_id, Member.reseller_id == row.reseller_id)
                              .values(**values))
        if res.rowcount != 1:
            await s.rollback()
            return None
        s.add(MemberTx(reseller_id=row.reseller_id, member_id=row.member_id, kind="payment", amount=extra))
        await s.commit()
    return extra


async def recent_builtin_topup(reseller_id: int, days: int = 3) -> int | None:
    """The NumberHub deposit id of this shop's newest built-in payment that can
    still matter (open, or looked at again for `days`): a replacement API key has
    to be able to read it, i.e. be on the same NumberHub account."""
    since = _now() - dt.timedelta(days=days)
    async with session_factory() as s:
        inv = await s.scalar(select(Payment.invoice_id).where(
            Payment.reseller_id == reseller_id, Payment.provider == "numberhub",
            Payment.invoice_id.is_not(None), Payment.created_at >= since)
            .order_by(Payment.id.desc()).limit(1))
    return int(inv) if inv and str(inv).isdigit() else None


def _paid_left_sum():
    # Never more than the balance, whatever a row says: it is what is still there.
    left = func.coalesce(Member.paid_left, 0)
    return func.coalesce(func.sum(case((left > Member.balance, Member.balance), else_=left)), 0)


async def any_qris_shop() -> bool:
    """Whether any shop takes QRIS payments (only then is an exchange rate needed)."""
    async with session_factory() as s:
        return (await s.scalar(select(func.count()).select_from(Reseller).where(
            Reseller.kasera_key_enc.is_not(None)))) > 0


async def builtin_unspent(reseller_id: int) -> Decimal:
    """What a shop's customers paid in with built-in payments and still hold
    (members.paid_left). The shop's NumberHub wallet has to cover it
    (payments.backing), and it is what the wallet service keeps for the shop."""
    async with session_factory() as s:
        total = await s.scalar(select(_paid_left_sum()).where(Member.reseller_id == reseller_id))
    return max(Decimal("0"), Decimal(str(total or 0))).quantize(Decimal("0.01"))


async def owner_paid_left(owner_id: int) -> Decimal:
    """The same over ALL of one owner's shops: they share one NumberHub wallet."""
    async with session_factory() as s:
        total = await s.scalar(select(_paid_left_sum()).where(Member.reseller_id.in_(
            select(Reseller.id).where(Reseller.owner_id == owner_id))))
    return max(Decimal("0"), Decimal(str(total or 0))).quantize(Decimal("0.01"))


async def builtin_figures(days: int = 3) -> list[dict]:
    """For every shop that was ever paid with built-in payments: what its customers
    still hold from them (as builtin_unspent), and the payments it has given to its
    customers in the last `days`, one by one (NumberHub's deposit number and the
    amount credited). One read, so they belong together. The wallet service keeps
    for the shop what customers hold plus every deposit not named here yet."""
    cent = Decimal("0.01")
    since = _now() - dt.timedelta(days=max(1, int(days)))
    async with session_factory() as s:
        paid = (await s.execute(
            select(Payment.reseller_id, Payment.member_id, func.coalesce(func.sum(Payment.credited), 0)).where(
                Payment.provider == "numberhub", Payment.status == Payment.PAID)
            .group_by(Payment.reseller_id, Payment.member_id))).all()
        if not paid:
            return []
        by_shop: dict[int, dict[int, Decimal]] = {}
        for rid, mid, total in paid:
            by_shop.setdefault(rid, {})[mid] = Decimal(str(total or 0))
        owners = dict((await s.execute(select(Reseller.id, Reseller.owner_id).where(
            Reseller.id.in_(list(by_shop))))).all())
        holds = dict((await s.execute(select(Member.reseller_id, _paid_left_sum()).where(
            Member.reseller_id.in_(list(by_shop))).group_by(Member.reseller_id))).all())
        given: dict[int, list[dict]] = {}
        for rid, invoice, credited in (await s.execute(
                select(Payment.reseller_id, Payment.invoice_id, Payment.credited).where(
                    Payment.provider == "numberhub", Payment.status == Payment.PAID,
                    Payment.invoice_id.is_not(None), Payment.paid_at >= since)
                .order_by(Payment.id.desc()).limit(5000))).all():
            if str(invoice).isdigit() and credited is not None:
                given.setdefault(rid, []).append({"id": int(invoice), "amount": str(Decimal(str(credited)).quantize(cent))})
    out = []
    for rid, per in sorted(by_shop.items()):
        if rid not in owners:
            continue
        hold = max(Decimal("0"), Decimal(str(holds.get(rid) or 0)))
        out.append({"shop_id": rid, "owner_id": owners[rid], "customers_hold": str(hold.quantize(cent)),
                    "credited": given.get(rid, []),
                    # The total, for a wallet service from before deposits were named one by one.
                    "deposits_seen": str(sum(per.values(), Decimal("0")).quantize(cent))})
    return out


async def pending_payments(limit: int = 500) -> list[Payment]:
    """Every link still waiting for the gateway, oldest first (for the poller)."""
    async with session_factory() as s:
        return list((await s.execute(select(Payment).where(
            Payment.status == Payment.PENDING, Payment.invoice_id.is_not(None))
            .order_by(Payment.id).limit(limit))).scalars())


async def recent_payments(reseller_id: int, limit: int = 8) -> list[Payment]:
    async with session_factory() as s:
        return list((await s.execute(select(Payment).where(
            Payment.reseller_id == reseller_id, Payment.status.in_([Payment.PAID, Payment.PENDING, Payment.MANUAL]),
            Payment.invoice_id.is_not(None)).order_by(Payment.id.desc()).limit(limit))).scalars())


async def payment_totals(reseller_id: int, days: int = 30) -> tuple[int, Decimal]:
    since = _now() - dt.timedelta(days=days)
    async with session_factory() as s:
        n, total = (await s.execute(select(func.count(Payment.id), func.coalesce(func.sum(Payment.credited), 0))
                                    .where(Payment.reseller_id == reseller_id, Payment.status == Payment.PAID,
                                           Payment.paid_at >= since))).one()
    return int(n or 0), Decimal(str(total or 0)).quantize(Decimal("0.01"))
