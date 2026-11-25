"""Statistics for the shop owner: the 📊 screen in the admin panel and a report every
morning, in the owner's own bot. Counts, sums and app names only."""
from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from app import repo
from app.models import Reseller
from app.texts import esc, money

log = logging.getLogger(__name__)
REPORT_HOUR_UTC = 5
# NumberHub's answers in words an owner can act on.
WHY = {
    "insufficient_funds": "your NumberHub wallet was empty",
    "rent_unavailable": "numbers to rent could not be priced at that moment",
    "esim_busy": "the eSIM plans could not be read at that moment", "changed": "that eSIM plan was withdrawn",
    "sold_out": "no number in stock", "purchase_failed": "no number in stock",
    "price_exceeded": "the price moved while they were buying",
    "duplicate_order": "too many open orders for that app and country",
    "spend_limit_reached": "your API key's daily limit was reached",
    "daily_spend_limit_exceeded": "your API key's daily limit was reached",
    "operators_unavailable": "no number in stock", "unknown_service": "that app is not sold any more",
    "unknown_package": "that package is not sold any more", "rate_limited": "too many requests at once",
    "bad_reply": "NumberHub did not answer",
    "no_answer": "NumberHub did not answer", "unknown": "not recorded",
}
PROVIDER = {"numberhub": "Built-in crypto", "oxapay": "Your OxaPay", "cryptobot": "Your @CryptoBot"}


def pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "–"


def report_on(reseller: Reseller) -> bool:
    return getattr(reseller, "daily_report", None) != 0


async def stats_text(reseller: Reseller, days: int = 30) -> str:
    r = await repo.shop_report(reseller.id, days)
    o = r["outcomes"]
    given = r["orders"] - o.get("failed", 0)
    lines = [f"📊 <b>Statistics</b> · last {days} days", "",
             f"Sold <b>{money(r['sales'])}</b> · your profit ≈ <b>{money(r['profit'])}</b>",
             f"Numbers given: <b>{given}</b> · codes delivered: <b>{r['codes']}</b> ({pct(r['codes'], given)})",
             f"No code (not charged): {o.get('canceled', 0) + o.get('expired', 0)} · purchase did not go through: {o.get('failed', 0)}"]
    if r["reasons"]:
        lines.append("Why purchases did not go through: " + " · ".join(
            f"{esc(WHY.get(why, why))} ×{n}" for why, n in r["reasons"]))
    if r["apps"]:
        lines += ["", "<b>Best apps</b> (codes of numbers given · sold · profit)"]
        for name, got_n, codes, sold, profit in r["apps"]:
            lines.append(f"{esc(name)}: {codes} of {got_n} ({pct(codes, got_n)}) · {money(sold)} · {money(profit)}")
    lines += ["", "<b>Customers</b>",
              f"All: {r['members']} · new: {r['new_members']} · opened the bot: {r['seen']} · bought: {r['buyers']}",
              f"On their balances now: {money(r['balances'])}"]
    lines += funnel_lines(await repo.funnel(reseller, days), days)
    paid_any = [p for p in r["payments"] if p[1]]
    if paid_any or r["deposits"][0]:
        lines += ["", "<b>Payments</b> (paid of opened · amount)"]
        for prov, opened, paid, total in paid_any:
            lines.append(f"{PROVIDER.get(prov, esc(prov))}: {paid} of {opened} ({pct(paid, opened)}) · {money(total)}")
        if r["deposits"][0]:
            lines.append(f"Approved by you: {r['deposits'][0]} · {money(r['deposits'][1])}")
    lines += ["", "🔔 Morning report: <b>" + ("on" if report_on(reseller) else "off") + "</b> "
              "(yesterday's sales in one message, only on days your shop was used)"]
    return "\n".join(lines)


FUNNEL_WORDS = (("open", "Opened the bot"), ("browse", "Looked at what you sell"), ("price", "Saw a price"),
                ("buy", "Tapped Buy"), ("short", "Were short of balance"), ("topup", "Opened Add balance"),
                ("pay", "Started a payment"), ("paid", "Paid"), ("bought", "Got what they bought"))


def funnel_lines(f: dict, days: int) -> list[str]:
    """How far the shop's customers got, one line a step. Nothing while no step was counted."""
    if not f.get("since"):
        return []
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    note = f", counted since {f['since']}" if f["since"] > start else ""
    return ["", f"<b>From a visit to a purchase</b> (customers{note})"] + [f"{words}: <b>{f[key]}</b>" for key, words in FUNNEL_WORDS]


# An owner is told that the wallet is nearly empty BEFORE a customer's purchase fails
# (until 2026-11-25 only after one did). Under this, or under what the shop's sales
# cost in the last 24 hours, and only while customers use the shop.
WALLET_LOW = Decimal("1.00")
WALLET_ACTIVE_DAYS = 3
WALLET_PAUSE_SEC = 1.0          # between two shops' wallet reads


def wallet_low_text(reseller: Reseller, available: Decimal, spent: Decimal, held: Decimal) -> str:
    from app import payments
    lines = [f"⚠️ <b>Your NumberHub wallet is low: {money(available)}.</b>",
             "Every sale is paid from it. When it is empty your customers cannot buy, even with money on "
             "their balance in this bot."]
    facts = []
    if spent > 0:
        facts.append(f"In the last 24 hours your sales cost <b>{money(spent)}</b> there.")
    if held > 0:
        facts.append(f"Your customers hold <b>{money(held)}</b> on their balances here.")
    if facts:
        lines.append(" ".join(facts))
    if payments.builtin_on(reseller):
        lines.append("What customers pay by built-in crypto lands in this wallet by itself. Money they pay you "
                     "another way does not.")
    elif payments.own(reseller) or reseller.deposit_info:
        lines.append("What customers pay through your own gateway, or to you directly, does not reach this "
                     "wallet: you add it yourself.")
    lines.append("Top up at numberhub.io or in @TheNumberHubBot.")
    return "\n".join(lines)


async def wallet_watch(now: dt.datetime | None = None) -> int:
    """Warn every owner whose wallet is low, once a day (kept across restarts).
    Returns how many were told."""
    import asyncio

    from app import runtime, selling
    from app.numberhub import NumberHubError, dec
    day, sent = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%d"), 0
    for reseller in await repo.list_resellers(status=Reseller.ACTIVE):
        try:
            key = f"wallet_low:{reseller.id}"
            if await repo.get_setting(key) == day or not await repo.customers_seen(reseller, WALLET_ACTIVE_DAYS):
                continue
            cli, bot = selling.client_for(reseller.id), runtime.bot_for(reseller.id)
            if cli is None or bot is None:
                continue
            try:
                available = dec((await cli.balance()).get("available"))
            except NumberHubError:
                continue                # not readable now: asked again on the next round
            finally:
                await asyncio.sleep(WALLET_PAUSE_SEC)
            spent, held = await repo.wallet_facts(reseller.id)
            if available >= max(WALLET_LOW, spent):
                continue
            await repo.set_setting(key, day)        # marked before it is sent: never twice a day
            await selling.send(bot, reseller.owner_id, wallet_low_text(reseller, available, spent, held))
            sent += 1
        except Exception:  # noqa: BLE001 — one shop's trouble never stops the rest
            log.exception("wallet check for reseller %s failed", reseller.id)
    return sent


async def daily_text(reseller: Reseller) -> str | None:
    """The last 24 hours for one shop, or None when nothing happened in it."""
    r = await repo.shop_report(reseller.id, 1)
    paid = sum(p[2] for p in r["payments"])
    if not r["orders"] and not paid and not r["new_members"]:
        return None
    o = r["outcomes"]
    lines = [f"🌅 <b>Your shop, the last 24 hours</b> · @{esc(reseller.bot_username or '')}",
             f"Sold <b>{money(r['sales'])}</b> · profit ≈ <b>{money(r['profit'])}</b> · {r['codes']} codes",
             f"New customers: {r['new_members']} · bought: {r['buyers']}"]
    if paid:
        total = sum((p[3] for p in r["payments"]), Decimal("0"))
        lines.append(f"Payments received: {paid} · {money(total)}")
    if o.get("failed"):
        lines.append("⚠️ Purchases that did not go through: " + " · ".join(
            f"{esc(WHY.get(why, why))} ×{n}" for why, n in r["reasons"]))
    lines.append("More in ⚙️ Admin panel → 📊 Statistics, where this report can be switched off.")
    return "\n".join(lines)


async def daily_once(now: dt.datetime | None = None) -> int:
    """Send the morning report to every owner it is due for. Returns how many went out."""
    from app import runtime
    from app.selling import send
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.hour < REPORT_HOUR_UTC:
        return 0
    day, sent = now.strftime("%Y-%m-%d"), 0
    for reseller in await repo.list_resellers(status=Reseller.ACTIVE):
        if not report_on(reseller) or reseller.report_day == day:
            continue
        try:
            if not await repo.claim_report_day(reseller.id, day):
                continue
            text = await daily_text(reseller)
            bot = runtime.bot_for(reseller.id)
            if text and bot is not None:
                await send(bot, reseller.owner_id, text)
                sent += 1
        except Exception:  # noqa: BLE001 — one shop's trouble never stops the rest
            log.exception("morning report for reseller %s failed", reseller.id)
    return sent
