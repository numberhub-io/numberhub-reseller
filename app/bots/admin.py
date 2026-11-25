"""The reseller's admin panel inside their own bot (owner only, English)."""
from __future__ import annotations

import asyncio
import inspect
import logging
import hashlib
import re
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app import payments, repo, selling
from app.bots.callbacks import Adm, Dep, Nav
from app.config import settings
from app.i18n import t
from app.models import Member, Reseller
from app.numberhub import NumberHubError, dec
from app.texts import esc, money

log = logging.getLogger(__name__)


def _cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Cancel", callback_data=Adm(a="home"))
    return kb.as_markup()


PRODUCT_LABEL = {"sms": "📱 Numbers", "email": "📧 Email codes", "proxy": "🌐 Proxy data",
                 "rent": "📅 Rented numbers", "esim": "🌍 eSIM"}


def commission_screen(reseller: Reseller, note: str = ""):
    """What the shop adds to NumberHub's price, product by product, with a button for each."""
    lines = ([note, ""] if note else []) + [
        "💲 <b>Commission</b>",
        "",
        "What you add on top of NumberHub's price. Each product can have its own commission. One that has "
        "none uses the commission for numbers.",
        "",
    ]
    kb = InlineKeyboardBuilder()
    for kind, label in PRODUCT_LABEL.items():
        now = pct(selling.commission(reseller, kind))
        same = "" if kind == "sms" or selling.own_commission(reseller, kind) is not None else " <i>(same as numbers)</i>"
        lines.append(f"{label}: <b>{now}%</b>{same}")
        kb.button(text=f"{label} · {now}%", callback_data=Adm(a="markup", arg=kind))
    cap = getattr(reseller, "max_profit", None)
    if cap is not None:
        lines += ["", f"💰 Your profit cap (<b>{money(cap)}</b>) holds numbers and every product that uses the "
                      "commission for numbers. A product with its own commission is priced by that commission alone."]
    lines += ["", "<i>A change applies to new purchases only.</i>"]
    kb.button(text="⬅️ Admin panel", callback_data=Adm(a="home"))
    kb.adjust(1)
    return "\n".join(lines), kb.as_markup()


def commission_prompt(reseller: Reseller, kind: str) -> str:
    now = pct(selling.commission(reseller, kind))
    if kind == "sms":
        return (PROMPTS["markup"].format(max=pct(settings.max_markup_pct), now=now, n=0)
                + "\n\nA product without its own commission follows this one.")
    same = "" if selling.own_commission(reseller, kind) is not None else " (same as numbers)"
    return (f"💲 <b>Commission for {PRODUCT_LABEL[kind]}</b>\n\nSend the percent you add on top of NumberHub's "
            f"price (0–{pct(settings.max_markup_pct)}). Now: <b>{now}%</b>{same}.\n"
            "Example: <code>15</code> — what costs you $10.00 sells for $11.50 and you earn $1.50.\n\n"
            "Send <code>-</code> to use the commission for numbers again.")


def _commission_cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Cancel", callback_data=Adm(a="markup"))
    return kb.as_markup()


async def dashboard(reseller: Reseller):
    day, week, month = [await repo.stats(reseller.id, d) for d in (1, 7, 30)]
    wallet = "—"
    cli = selling.client_for(reseller.id)
    if cli is not None:
        try:
            bal = await cli.balance()
            wallet = f"<b>{money(dec(bal.get('available')))}</b> available"
        except NumberHubError as exc:
            wallet = "⚠️ API key rejected" if exc.status in (401, 403) else "unavailable right now"

    def line(label, s):
        profit = s["sales"] - s["cost"]
        return f"📊 {label}: <b>{s['orders']}</b> sold · {money(s['sales'])} · profit ≈ <b>{money(profit)}</b>"

    commission = pct(reseller.markup_pct)
    # A worked example from a real route when its price is known (custom prices and
    # the cap included), else a $1.00 number at the shop's commission.
    ex = selling.price_example(reseller.id, "wa")
    if ex is not None:
        nh, cust = ex
        example = (f"<i>e.g. 💬 WhatsApp: NumberHub {money(nh)} → your customers {money(cust)} "
                   f"→ you earn {money(cust - nh)}</i>")
    else:
        cust = selling.shop_price(reseller, {}, "", "", Decimal("1.00"))
        example = f"<i>e.g. a $1.00 number sells for {money(cust)} → you earn {money(cust - Decimal('1.00'))}</i>"
    own = [f"{label} <b>{pct(selling.commission(reseller, kind))}%</b>" for kind, label in PRODUCT_LABEL.items()
           if selling.own_commission(reseller, kind) is not None]
    n_rules = await repo.count_price_rules(reseller.id)
    extras = []
    if getattr(reseller, "max_profit", None) is not None:
        extras.append(f"💰 Most you earn on one number: <b>{money(reseller.max_profit)}</b>")
    if n_rules:
        extras.append(f"🏷 Custom prices: <b>{n_rules}</b>")
    waiting = len(await repo.pending_deposits(reseller.id, limit=100))
    if waiting:
        extras.append(f"💳 Deposits waiting for you: <b>{waiting}</b>")
    gates = payments.connected(reseller)
    if gates:
        n_paid, total = await repo.payment_totals(reseller.id, 30)
        extras.append(f"💳 Automatic payments: <b>{' + '.join(payments.LABEL[g] for g in gates)}</b>"
                      + (f" · {n_paid} paid, {money(total)} in 30 days" if n_paid else ""))
    else:
        extras.append("💳 Automatic payments: <b>off</b> (turn on built-in crypto, or connect your own "
                      "OxaPay or @CryptoBot, in 💳 Payments)")

    text = "\n".join([
        f"⚙️ <b>Admin panel</b> · @{esc(reseller.bot_username)}",
        "",
        f"👥 Customers: <b>{day['members']}</b>",
        line("Today", day), line("7 days", week), line("30 days", month),
        "",
        f"💲 Your commission: <b>{commission}%</b> on top of NumberHub's price",
        f"     {example}",
        *([f"     Own commission: {' · '.join(own)}"] if own else []),
        *extras,
        f"🏦 NumberHub wallet: {wallet}",
        f"🔗 Share your bot: <code>t.me/{esc(reseller.bot_username)}</code>",
        "",
        # One line for what the shop sells. Until 2026-11-25 each product had a paragraph
        # here, 1,500 characters on every visit; what each one is says its switch when tapped.
        " · ".join(f"{label}: <b>{'on' if getattr(reseller, field, None) != 0 else 'off'}</b>" for label, field in
                   (("📧 Email codes", "email_on"), ("🌐 Proxy data", "proxy_on"), ("🌍 eSIM", "esim_on"),
                    ("📅 Rented numbers", "rent_on"))),
        "",
        "<i>Every sale is paid from your NumberHub wallet. Customers add balance by themselves: 💳 Payments.</i>",
    ])
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Statistics", callback_data=Adm(a="stats"))
    kb.button(text="➕ Add balance", callback_data=Adm(a="add"))
    kb.button(text="➖ Remove balance", callback_data=Adm(a="remove"))
    kb.button(text="👥 Customers", callback_data=Adm(a="members"))
    kb.button(text="📣 Broadcast", callback_data=Adm(a="broadcast"))
    kb.button(text="💲 Commission", callback_data=Adm(a="markup"))
    kb.button(text="🏷 Custom prices", callback_data=Adm(a="prices"))
    kb.button(text="💳 Payments" + (f" ({waiting})" if waiting else ""), callback_data=Adm(a="deposits"))
    kb.button(text="📝 Welcome text", callback_data=Adm(a="welcome"))
    kb.button(text="🆘 Support contact", callback_data=Adm(a="support"))
    kb.button(text="🚫 Block / unblock", callback_data=Adm(a="block"))
    # Email codes: a mailbox for one verification mail, sold like a number.
    email_on = getattr(reseller, "email_on", None) != 0
    kb.button(text="📧 Email codes: on" if email_on else "📧 Email codes: off",
              callback_data=Adm(a="email", arg="off" if email_on else "on"))
    # Proxy data packages: paid up front, sold at your commission like everything else.
    proxy_on = getattr(reseller, "proxy_on", None) != 0
    kb.button(text="🌐 Proxy data: on" if proxy_on else "🌐 Proxy data: off",
              callback_data=Adm(a="proxy", arg="off" if proxy_on else "on"))
    # Rented numbers: a number for days, paid up front, refundable only in its first minutes.
    rent_on = getattr(reseller, "rent_on", None) != 0
    kb.button(text="📅 Rented numbers: on" if rent_on else "📅 Rented numbers: off",
              callback_data=Adm(a="rent", arg="off" if rent_on else "on"))
    # eSIM data plans: paid up front, delivered as a QR code.
    esim_on = getattr(reseller, "esim_on", None) != 0
    kb.button(text="🌍 eSIM: on" if esim_on else "🌍 eSIM: off",
              callback_data=Adm(a="esim", arg="off" if esim_on else "on"))
    kb.button(text="🔄 Refresh", callback_data=Adm(a="home"))
    kb.button(text="🏠 Menu", callback_data=Nav(to="menu"))
    kb.adjust(1, 2, 2, 2, 2, 2, 2, 2, 2)
    return text, kb.as_markup()


async def stats_screen(reseller: Reseller):
    from app import reports
    kb = InlineKeyboardBuilder()
    on = reports.report_on(reseller)
    kb.button(text="🔕 Morning report off" if on else "🔔 Morning report on",
              callback_data=Adm(a="report", arg="off" if on else "on"))
    kb.button(text="⬅️ Admin panel", callback_data=Adm(a="home"))
    kb.adjust(1)
    return await reports.stats_text(reseller, 30), kb.as_markup()


MAX_RULES = 200
MAX_FIXED = Decimal("1000")


def rule_text(r) -> str:
    where = esc((r.country_name or r.country)[:40]) if r.country else "all countries"
    what = f"{money(r.value)} fixed" if r.mode == "fixed" else f"{pct(r.value)}% commission"
    return f"{esc((r.service_name or r.service)[:40])} · {where}: <b>{what}</b>"


PRICE_PAGE = 20     # rules on one page of the price screen


async def prices_screen(reseller: Reseller, note: str = "", page: int = 0):
    """The shop's custom prices, PRICE_PAGE to a page. Until 2026-11-25 all of them
    (up to MAX_RULES) were printed on one screen: past about 90 the text was longer
    than Telegram takes, and the owner could neither see nor remove a rule."""
    rules = await repo.price_rules(reseller.id)
    cap = getattr(reseller, "max_profit", None)
    lines = ([note, ""] if note else []) + [
        "🏷 <b>Custom prices</b>",
        "",
        f"Every number sells at NumberHub's price + your commission (<b>{pct(reseller.markup_pct)}%</b>)"
        + (f", and you earn at most <b>{money(cap)}</b> on one number." if cap is not None else "."),
        "Give any app, or an app in one country, its own price: a fixed price or its own commission.",
        "<i>A fixed price never sells below NumberHub's price: if NumberHub's price goes above it, "
        "that number sells at NumberHub's price and you earn nothing on it, but you never pay for it.</i>",
        "",
    ]
    kb = InlineKeyboardBuilder()
    pages = max(1, -(-len(rules) // PRICE_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = rules[page * PRICE_PAGE:(page + 1) * PRICE_PAGE]
    n = len(chunk)
    if rules:
        if pages > 1:
            first = page * PRICE_PAGE + 1
            lines += [f"<b>{first}–{first + n - 1} of {len(rules)}</b>" if n > 1 else f"<b>{first} of {len(rules)}</b>", ""]
        for i, r in enumerate(chunk, page * PRICE_PAGE + 1):
            lines.append(f"{i}. {rule_text(r)}")
            kb.button(text=f"🗑 {i}", callback_data=Adm(a="pr_del", arg=f"{r.id}|{page}"))
    else:
        lines.append("No custom prices yet.")
    nav = 0
    if pages > 1:
        if page > 0:
            kb.button(text="◀️", callback_data=Adm(a="prices", arg=str(page - 1)))
            nav += 1
        kb.button(text=f"{page + 1}/{pages}", callback_data=Adm(a="prices", arg=str(page)))
        nav += 1
        if page < pages - 1:
            kb.button(text="▶️", callback_data=Adm(a="prices", arg=str(page + 1)))
            nav += 1
    kb.button(text="➕ Add a custom price", callback_data=Adm(a="pr_add"))
    kb.button(text="💰 Profit cap", callback_data=Adm(a="cap"))
    kb.button(text="⬅️ Admin panel", callback_data=Adm(a="home"))
    kb.adjust(*([5] * (n // 5) + ([n % 5] if n % 5 else [])), *([nav] if nav else []), 1, 1, 1)
    return "\n".join(lines), kb.as_markup()


def deposit_card(dep, member: Member | None, status_line: str = "") -> str:
    who = (f"<code>{member.telegram_id}</code> " + (f"@{esc(member.username)} " if member.username else "")
           + esc(member.full_name or "")) if member else "?"
    lines = [f"💳 <b>Deposit #{dep.number}</b> · <b>{money(dep.amount)}</b>", f"From: {who}"]
    if member is not None:
        lines.append(f"Their balance now: {money(member.available)}")
    if dep.proof_text:
        lines.append(f"Proof: <code>{esc(dep.proof_text)}</code>")
    if dep.proof_photo:
        lines.append("Proof: the screenshot above")
    if status_line:
        lines += ["", status_line]
    return "\n".join(lines)


def deposit_buttons(dep):
    kb = InlineKeyboardBuilder()
    kb.button(text=f"✅ Approve {money(dep.amount)}", callback_data=Dep(a="ok", id=dep.id))
    kb.button(text="✏️ Other amount", callback_data=Dep(a="amt", id=dep.id))
    kb.button(text="❌ Reject", callback_data=Dep(a="no", id=dep.id))
    kb.adjust(1, 2)
    return kb.as_markup()


async def notify_owner_deposit(bot, reseller: Reseller, member: Member, dep) -> None:
    """The owner gets the request in their own bot, with the screenshot if there is one."""
    text = deposit_card(dep, member)
    try:
        if dep.proof_photo:
            await bot.send_photo(reseller.owner_id, dep.proof_photo, caption=text, reply_markup=deposit_buttons(dep))
        else:
            await bot.send_message(reseller.owner_id, text, reply_markup=deposit_buttons(dep))
    except Exception:  # noqa: BLE001 — the request is saved: it also waits in ⚙️ Admin → 💳 Payments
        log.warning("reseller %s: deposit #%s notice not delivered to the owner", reseller.id, dep.number)


async def settle_deposit(bot, reseller: Reseller, deposit_id: int, approve: bool, amount: Decimal | None = None):
    """Approve or reject, then tell the customer. None = it was already decided."""
    dep = await repo.decide_deposit(reseller.id, deposit_id, approve, amount)
    if dep is None:
        return None
    member = await repo.get_member(dep.member_id)
    if member is not None:
        lang = member.language or "en"
        if approve:
            msg = t(lang, "dep_approved", n=dep.number, amount=money(dep.credited), balance=money(member.available))
        else:
            from app.bots.reseller import support_label
            msg = t(lang, "dep_rejected", n=dep.number, amount=money(dep.amount),
                    support=support_label(reseller, lang))
        from app.texts import after_credit_kb
        await selling.send(bot, member.telegram_id, msg, after_credit_kb(member) if approve else None)
    return dep, member


def _status_word(p) -> str:
    return {"paid": "✅", "pending": "⏳", "manual": "✋"}.get(p.status, "•")


async def deposits_screen(reseller: Reseller, note: str = ""):
    rows = await repo.pending_deposits(reseller.id, limit=10)
    on = bool(reseller.deposit_info)
    gates = payments.connected(reseller)
    builtin = payments.builtin_on(reseller)
    lines = ([note, ""] if note else []) + [
        "💳 <b>Payments</b>",
        "",
        "<b>💵 Built-in crypto</b> " + ("✅ on" if builtin else "⏸ off" if payments.builtin_allowed(reseller) else
                                       "— not available: this shop runs on an API key you pasted yourself. "
                                       "Connect the bot again in @TheNumberHubBot (🤖 Your own bot) to use it."),
        "Nothing to set up. A customer taps 💳 Add balance, picks an amount and a coin (USDT, BTC, ETH, LTC, "
        "TRX, BNB, SOL, USDC and more) and gets an address and the exact amount to send. When it arrives, "
        "the money is added in dollars to <b>your NumberHub wallet</b> and "
        "the same amount to their balance here, so your customers' payments are what your shop sells with. "
        "What you earn stays in your NumberHub wallet.",
        "",
        "<b>⚡ Your own account (crypto)</b>",
        "Customers tap 💳 Add balance, pick an amount and pay a link. Their balance goes up by itself "
        "when the payment arrives, and the money goes straight to YOUR own OxaPay or @CryptoBot account.",
        f"🪙 OxaPay: <b>{'✅ connected' if payments.OXAPAY in gates else '— not connected'}</b>"
        " · BTC, ETH, USDT and many more coins",
        f"🤖 @CryptoBot: <b>{'✅ connected' if payments.CRYPTOBOT in gates else '— not connected'}</b>"
        " · USDT, TON, BTC and more, inside Telegram",
        "🇮🇩 KaseraPay (QRIS): <b>" + (
            f"✅ connected · {_kasera_mode(reseller)}: {rp(payments.kasera_rate(reseller))} = $1"
            if payments.KASERA in gates else
            "⚠️ today's rate could not be read yet: QRIS starts by itself as soon as it is"
            if payments.has_key(reseller, payments.KASERA) and payments.kasera_auto(reseller) else
            "⚠️ key saved: set the rupiah rate" if payments.has_key(reseller, payments.KASERA) else "— not connected")
        + "</b> · customers in Indonesia pay by QR from any bank or wallet app",
    ]
    if (payments.KASERA in gates and not payments.kasera_auto(reseller)
            and payments.kasera_rate_odd(payments.kasera_rate(reseller))):
        lines.append(kasera_rate_warning(payments.kasera_rate(reseller)))
    recent = await repo.recent_payments(reseller.id, limit=6) if gates else []
    if recent:
        n_paid, total = await repo.payment_totals(reseller.id, 30)
        lines += ["", f"Last 30 days: <b>{n_paid}</b> paid, <b>{money(total)}</b>"]
        for p in recent:
            mbr = await repo.get_member(p.member_id)
            tag = f"@{mbr.username}" if mbr and mbr.username else (str(mbr.telegram_id) if mbr else "?")
            when = p.paid_at or p.created_at
            lines.append(f"{_status_word(p)} {money(p.amount)} · {payments.LABEL.get(p.provider, p.provider)} · "
                         f"{esc(tag)} · {when:%d %b %H:%M}")
    lines += [
        "",
        "<b>✍️ Manual</b>",
        ("✅ On. Customers can also pay you your way, then send the amount and a transaction ID or "
         "screenshot. You approve here, and their balance goes up at once."
         if on else "⏸ Off. Set your payment details to let customers pay you your way and send proof."),
    ]
    if on:
        lines += ["", "<b>What customers see:</b>", esc(reseller.deposit_info)]
    if reseller.deposit_min:
        lines.append(f"Minimum top-up: <b>{money(reseller.deposit_min)}</b>")
    if gates:
        low, high = payments.limits(reseller)
        lines += ["", f"<b>Automatic top-ups here:</b> from <b>{money(low)}</b> to <b>{money(high)}</b> at a time"
                      " (QRIS from $0.50, crypto from $1.00; your own minimum counts when it is higher)."]
    if getattr(reseller, "pay_note", None):
        lines += ["", "<b>Your note on the top-up screen:</b>", esc(reseller.pay_note)]
    lines += ["", f"<b>Waiting for you:</b> {len(rows)}" if rows else "No manual deposit waiting."]
    kb = InlineKeyboardBuilder()
    sizes = []
    if payments.builtin_allowed(reseller):
        kb.button(text="⏸ Turn off built-in crypto" if builtin else "💵 Turn on built-in crypto",
                  callback_data=Adm(a="nhpay", arg="off" if builtin else "on"))
        sizes.append(1)
    for g, label in ((payments.OXAPAY, "🪙 OxaPay"), (payments.CRYPTOBOT, "🤖 @CryptoBot"),
                     (payments.KASERA, "🇮🇩 KaseraPay")):
        if payments.has_key(reseller, g):
            kb.button(text=f"🗑 Remove {label.split(' ', 1)[1]}", callback_data=Adm(a="pay_off", arg=g))
        else:
            kb.button(text=f"🔑 Connect {label}", callback_data=Adm(a="pay_on", arg=g))
    sizes += [2, 1]
    if payments.has_key(reseller, payments.KASERA):
        kb.button(text="💱 Rupiah for $1", callback_data=Adm(a="kasera_rate"))
        sizes.append(1)
    for d in rows:
        member = await repo.get_member(d.member_id)
        tag = f"@{member.username}" if member and member.username else (str(member.telegram_id) if member else "?")
        lines.append(f"#{d.number} · {money(d.amount)} · {esc(tag)}"
                     + (f" · <code>{esc(d.proof_text[:40])}</code>" if d.proof_text else "")
                     + (" · 🖼" if d.proof_photo else ""))
        kb.button(text=f"✅ #{d.number} {money(d.amount)}", callback_data=Dep(a="ok", id=d.id))
        kb.button(text=f"❌ #{d.number}", callback_data=Dep(a="no", id=d.id))
        sizes.append(2)
    kb.button(text="✏️ Payment details", callback_data=Adm(a="dep_info"))
    kb.button(text="📉 Minimum", callback_data=Adm(a="dep_min"))
    kb.button(text="📝 Note for customers", callback_data=Adm(a="pay_note"))
    if on:
        # One tap instead of sending "-" as the payment details.
        kb.button(text="🗑 Turn off manual deposits", callback_data=Adm(a="dep_off"))
    kb.button(text="⬅️ Admin panel", callback_data=Adm(a="home"))
    kb.adjust(*sizes, 2, *([1] if on else []), 1)
    return "\n".join(lines), kb.as_markup()


def rp(n) -> str:
    return "Rp " + f"{int(n):,}".replace(",", ".")


def _kasera_mode(reseller: Reseller) -> str:
    if not payments.kasera_auto(reseller):
        return "your own rate"
    extra = Decimal(getattr(reseller, "kasera_extra", None) or 0)
    return "today's rate" + (f" + {extra.normalize():f}%" if extra else "")


def kasera_rate_screen(reseller: Reseller, note: str = ""):
    """How dollars turn into rupiah for QRIS in this shop: today's rate by itself
    (the default), with a percent on top if the owner wants, or a rate they type."""
    from app import fx
    auto, rate, market = payments.kasera_auto(reseller), payments.kasera_rate(reseller), fx.current()
    lines = ([note, ""] if note else []) + ["💱 <b>Rupiah for $1</b>", ""]
    if rate is not None:
        lines.append(f"Now: <b>{rp(rate)} = $1</b> ({_kasera_mode(reseller)}). A customer who adds $5.00 pays "
                     f"<b>{rp(rate * 5)}</b> by QRIS.")
    elif auto:
        lines.append("Today's rate could not be read yet. QRIS starts by itself as soon as it is.")
    else:
        lines.append("No rate is set yet.")
    if market is not None:
        lines.append(f"Today a dollar costs <b>{rp(market)}</b>.")
    if not auto and payments.kasera_rate_odd(rate):
        lines += ["", kasera_rate_warning(rate)]
    lines += ["", "<b>📈 Today's rate</b>: read by itself a few times a day; nothing to type. You can add a "
                  "percent on top, for example to cover KaseraPay's fee.",
              "<b>✏️ Your own rate</b>: a fixed number of rupiah for one dollar, until you change it.",
              "", "<i>A QR already shown keeps the rate it was made with.</i>"]
    kb = InlineKeyboardBuilder()
    if not auto:
        kb.button(text="📈 Use today's rate", callback_data=Adm(a="kasera_auto"))
    else:
        kb.button(text="➕ Percent on top", callback_data=Adm(a="kasera_extra"))
    kb.button(text="✏️ Set my own rate", callback_data=Adm(a="kasera_own"))
    kb.button(text="⬅️ Payments", callback_data=Adm(a="deposits"))
    kb.adjust(1)
    return "\n".join(lines), kb.as_markup()


GATEWAY_HOWTO = {
    payments.OXAPAY: (
        "🪙 <b>Connect OxaPay</b>\n\n"
        "1. Sign up at <b>oxapay.com</b>.\n"
        "2. In your dashboard, create a <b>Merchant API key</b> (Merchant Service) and copy it.\n"
        "3. Send the key here.\n\n"
        "Customers then pay in BTC, ETH, USDT and many more coins; the gateway fee is added to what "
        "they pay, so you get the full amount in your OxaPay balance.\n\n"
        "<i>Your message is deleted at once and the key is stored encrypted. Send <code>-</code> to cancel.</i>"),
    payments.CRYPTOBOT: (
        "🤖 <b>Connect @CryptoBot</b>\n\n"
        "1. Open @CryptoBot → <b>Crypto Pay</b> → <b>Create App</b>.\n"
        "2. Open the app → <b>API Token</b> and copy it.\n"
        "3. Send the token here.\n\n"
        "Customers then pay inside Telegram with USDT, TON, BTC and more, and the money goes to your "
        "@CryptoBot wallet.\n\n"
        "<i>Your message is deleted at once and the token is stored encrypted. Send <code>-</code> to cancel.</i>"),
    payments.KASERA: (
        "🇮🇩 <b>Connect KaseraPay (QRIS)</b>\n\n"
        "1. Sign up at <b>pay.kasera.id</b> and verify your account (an unverified account can take only "
        "a few payments).\n"
        "2. In your dashboard open <b>Developer</b>, create an API key and copy it. A test key "
        "(<code>kp_test_…</code>) lets you try the whole flow first: test payments add no balance.\n"
        "3. Send the key here. Next you set how many rupiah one dollar costs in your shop.\n\n"
        "Customers then pay by QR from any Indonesian bank or wallet app, and the money goes to your "
        "KaseraPay balance. KaseraPay takes its fee from you unless you switch \"buyer pays\" on in its "
        "dashboard. One payment can be up to Rp 1.000.000.\n\n"
        "<i>Your message is deleted at once and the key is stored encrypted. Send <code>-</code> to cancel.</i>"),
}


KASERA_RATE_ASK = ("💱 <b>How many rupiah is one dollar in your shop?</b>\n\nSend a number, for example "
                   "<code>16500</code>. A customer who adds $5.00 then pays Rp 82.500 by QRIS. You can change "
                   "it any time; a QR already shown keeps the rate it was made with.")


def kasera_rate_warning(rate) -> str:
    """What an unusual rupiah rate means, in the owner's own numbers. Their price
    stays theirs: nothing is refused."""
    low, high = payments.Kasera.USUAL_RATE
    rp = lambda n: "Rp " + f"{int(n):,}".replace(",", ".")      # noqa: E731
    if Decimal(rate) < low:
        return (f"⚠️ <b>Check this rate.</b> A dollar usually costs well over {rp(low)}. At yours, a customer "
                f"who pays {rp(rate)} gets $1.00 of balance. If that is not what you mean, change it with "
                f"💱 Rupiah for $1.")
    return (f"⚠️ <b>Check this rate.</b> A dollar usually costs well under {rp(high)}. At yours, a customer "
            f"pays {rp(rate)} for $1.00 of balance. If that is not what you mean, change it with "
            f"💱 Rupiah for $1.")


def _back_to_prices():
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Cancel", callback_data=Adm(a="prices"))
    return kb


async def app_picker(reseller: Reseller, query: str = ""):
    """Popular apps as buttons, or the apps whose name matches what was typed."""
    from app.catalog_ui import POPULAR, icon, nice_name
    kb = InlineKeyboardBuilder()
    if query:
        q = query.strip().lower()
        items = await selling.services(reseller)
        hits = [s for s in items if q == s["code"].lower() or q in nice_name(s["code"], s["name"]).lower()]
        hits.sort(key=lambda s: (not nice_name(s["code"], s["name"]).lower().startswith(q),
                                 len(s["name"])))
        for s in hits[:12]:
            kb.button(text=f"{icon(s['code'])} {nice_name(s['code'], s['name'])}"[:60],
                      callback_data=Adm(a="pr_app", arg=s["code"]))
        text = (f"🏷 Apps matching <b>{esc(query)}</b>:" if hits
                else f"❌ No app matches <b>{esc(query)}</b>. Send another name.")
    else:
        for code, emoji, name in POPULAR[:12]:
            kb.button(text=f"{emoji} {name}", callback_data=Adm(a="pr_app", arg=code))
        text = "🏷 <b>Which app?</b>\n\nTap one, or send the app's name (for example <code>tiktok</code>)."
    kb.button(text="✖️ Cancel", callback_data=Adm(a="prices"))
    kb.adjust(2)
    return text, kb.as_markup()


PICK_PAGE = 20


async def country_picker(reseller: Reseller, service: str, query: str = "", page: int = 0):
    """All countries, or one. Every country of the app, page by page (the ones
    with numbers first, as customers see them), each with NumberHub's price; or
    the ones whose name matches what was typed. Before 2026-10-03 only the first
    16 in-stock ones were listed and the rest looked unavailable."""
    from app.catalog_ui import nice_name
    name = nice_name(service, selling.service_name(await selling.services(reseller), service))
    kb = InlineKeyboardBuilder()
    try:
        rows = await selling.countries(reseller, service)
    except selling.SellError:
        rows = []
    if query:
        q = query.strip().lower()
        rows = [r for r in rows if q in str(r.get("name") or "").lower()
                or (len(q) == 2 and q == str(r.get("flag") or "").lower())]
        shown, pages = rows[:PICK_PAGE], 1
    else:
        kb.button(text="🌍 All countries", callback_data=Adm(a="pr_cty", arg=f"{service}|*"))
        pages = max(1, -(-len(rows) // PICK_PAGE))
        page = min(max(0, page), pages - 1)
        shown = rows[page * PICK_PAGE:(page + 1) * PICK_PAGE]
    for r in shown:
        mark = "" if r.get("in_stock") else " ⏳"
        kb.button(text=f"{r.get('emoji', '')} {r.get('name')} · NumberHub {money(r['ceiling'])}{mark}"[:60],
                  callback_data=Adm(a="pr_cty", arg=f"{service}|{r['country']}"))
    sizes = [1] * ((0 if query else 1) + len(shown))
    if pages > 1:
        if page > 0:
            kb.button(text="◀️", callback_data=Adm(a="pr_pg", arg=f"{service}|{page - 1}"))
        kb.button(text=f"{page + 1}/{pages}", callback_data=Adm(a="pr_pg", arg=f"{service}|{page}"))
        if page < pages - 1:
            kb.button(text="▶️", callback_data=Adm(a="pr_pg", arg=f"{service}|{page + 1}"))
        sizes.append(1 + (page > 0) + (page < pages - 1))
    kb.button(text="✖️ Cancel", callback_data=Adm(a="prices"))
    sizes.append(1)
    kb.adjust(*sizes)
    if query and not rows:
        text = f"❌ No country matches <b>{esc(query)}</b> for {esc(name)}. Send another name."
    else:
        text = (f"🏷 <b>{esc(name)}</b>: one price for all countries, or for one country?\n\n"
                f"Tap one ({len(rows)} countries; ⏳ = no number right now), or send a country name.")
    return text, kb.as_markup()


async def value_prompt(reseller: Reseller, service: str, country: str):
    from app.catalog_ui import nice_name
    name = nice_name(service, selling.service_name(await selling.services(reseller), service))
    try:
        rows = await selling.countries(reseller, service)
    except selling.SellError:
        rows = []
    if country:
        row = next((r for r in rows if str(r["country"]) == country), None)
        where = f"{row.get('emoji', '')} {esc(row.get('name'))}" if row else esc(country)
        now = (f"NumberHub's price now: <b>{money(row['ceiling'])}</b> · your customers pay now: "
               f"<b>{money(row['member_price'])}</b>") if row else "This country has no number right now."
    else:
        where = "all countries"
        if rows:
            low, high = min(r["ceiling"] for r in rows), max(r["ceiling"] for r in rows)
            now = f"NumberHub's prices for {esc(name)}: <b>{money(low)}</b> to <b>{money(high)}</b> by country."
        else:
            now = "No country has a number right now."
    text = (f"🏷 <b>{esc(name)}</b> · {where}\n{now}\n\n"
            "Send your price:\n• a fixed price, for example <code>0.35</code>\n"
            f"• or a commission, for example <code>10%</code> (0–{pct(settings.max_markup_pct)}%)")
    return text, _back_to_prices().as_markup()


def parse_price(raw: str) -> tuple[str, Decimal] | None:
    """'0.35' / '$0.35' -> fixed; '10%' -> commission. None when it isn't one."""
    text = (raw or "").strip().replace(",", ".").replace("$", "").replace(" ", "")
    mode = "pct" if text.endswith("%") else "fixed"
    try:
        v = Decimal(text.rstrip("%"))
    except (InvalidOperation, ValueError):
        return None
    if not v.is_finite():
        return None
    if mode == "pct" and not Decimal("0") <= v <= settings.max_markup_pct:
        return None
    if mode == "fixed" and not Decimal("0.01") <= v <= MAX_FIXED:
        return None
    return mode, v.quantize(Decimal("0.01"))


AMBIGUOUS = ("❌ More than one of your customers has used that username. Send their ID instead "
             "(they find it under 💰 Balance; 👥 Customers lists it too).")


def pct(v: Decimal) -> str:
    """30 -> '30', 12.50 -> '12.5' (never '3E+1')."""
    return f"{Decimal(v).normalize():f}"


def _bc_mark(text: str) -> str:
    """Which broadcast a Send button belongs to: an old button never sends a newer text."""
    return hashlib.blake2s(str(text).encode(), digest_size=4).hexdigest()


PROMPTS = {
    "add": "➕ <b>Add balance</b>\n\nSend the customer's ID (or @username) and the amount.\nExample: <code>123456789 5</code>\n\n<i>Customers find their ID under 💰 Balance.</i>",
    "remove": "➖ <b>Remove balance</b>\n\nSend the customer's ID (or @username) and the amount.\nExample: <code>123456789 2.50</code>",
    "markup": "💲 <b>Your commission</b>\n\nSend the percent you add on top of NumberHub's price (0–{max}). Now: <b>{now}%</b>.\nExample: <code>30</code> — a $1.00 number sells for $1.30 and you earn $0.30.",
    "welcome": "📝 <b>Welcome text</b>\n\nSend the text your customers see in the menu (up to 800 characters).\nSend <code>-</code> to go back to the default text.",
    "support": "🆘 <b>Support contact</b>\n\nSend your contact for questions and top-ups: an @username or a link (https://…).",
    "broadcast": "📣 <b>Broadcast</b>\n\nSend the message to deliver to all <b>{n}</b> customers.",
    "block": "🚫 <b>Block / unblock</b>\n\nSend the customer's ID or @username. A blocked customer can't use the bot; send it again to unblock.",
}


def register(r: Router) -> None:
    from app.bots.reseller import AdminForm, parse_amount

    def owner_only(handler):
        # aiogram hands a **kwargs handler every context value; pass the wrapped
        # handler only the ones it declares.
        wanted = set(inspect.signature(handler).parameters)

        async def wrapped(event, **kw):
            if not kw.get("is_owner"):
                if isinstance(event, CallbackQuery):
                    await event.answer()
                return None
            return await handler(event, **{k: v for k, v in kw.items() if k in wanted})
        wrapped.__name__ = handler.__name__
        return wrapped

    async def reply(m: Message, screen) -> None:
        """Send a (text, keyboard) screen as a new message."""
        text, kb = screen
        await m.answer(text, reply_markup=kb, disable_web_page_preview=True)

    async def show(target, text, kb=None):
        from app.bots.reseller import _show
        await _show(target, text, kb)

    @r.message(Command("admin"))
    @owner_only
    async def cmd_admin(m: Message, reseller: Reseller, state: FSMContext, is_owner: bool = False):
        await state.clear()
        await show(m, *await dashboard(reseller))

    @r.callback_query(Adm.filter())
    @owner_only
    async def adm(c: CallbackQuery, callback_data: Adm, reseller: Reseller, state: FSMContext,
                  is_owner: bool = False):
        a = callback_data.a
        # The broadcast waiting for its Send button lives in the session: read before it is wiped.
        draft = (await state.get_data()).get("bc_text") if a == "bc_send" else None
        await state.clear()
        if a == "home":
            await show(c, *await dashboard(reseller))
        elif a == "stats":
            await show(c, *await stats_screen(reseller))
        elif a == "email" and callback_data.arg in ("on", "off"):
            await repo.update_reseller(reseller.id, email_on=1 if callback_data.arg == "on" else 0)
            await show(c, *await dashboard(await repo.get_reseller(reseller.id)))
            await c.answer("📧 Email codes are on: your customers see the button in the menu."
                           if callback_data.arg == "on" else
                           "📧 Email codes are off. A mailbox a customer already has still works.", show_alert=True)
            return
        elif a == "proxy" and callback_data.arg in ("on", "off"):
            await repo.update_reseller(reseller.id, proxy_on=1 if callback_data.arg == "on" else 0)
            await show(c, *await dashboard(await repo.get_reseller(reseller.id)))
            await c.answer("🌐 Proxy data is on: your customers see the button in the menu."
                           if callback_data.arg == "on" else
                           "🌐 Proxy data is off. A package a customer already bought still works.", show_alert=True)
            return
        elif a == "esim" and callback_data.arg in ("on", "off"):
            await repo.update_reseller(reseller.id, esim_on=1 if callback_data.arg == "on" else 0)
            await show(c, *await dashboard(await repo.get_reseller(reseller.id)))
            await c.answer("🌍 eSIM is on: your customers see the button in the menu."
                           if callback_data.arg == "on" else
                           "🌍 eSIM is off. An eSIM a customer already bought still opens.", show_alert=True)
            return
        elif a == "rent" and callback_data.arg in ("on", "off"):
            await repo.update_reseller(reseller.id, rent_on=1 if callback_data.arg == "on" else 0)
            await show(c, *await dashboard(await repo.get_reseller(reseller.id)))
            await c.answer("📅 Rented numbers are on: your customers see the button in the menu."
                           if callback_data.arg == "on" else
                           "📅 Rented numbers are off. A number a customer already rents keeps working.", show_alert=True)
            return
        elif a == "report" and callback_data.arg in ("on", "off"):
            await repo.update_reseller(reseller.id, daily_report=1 if callback_data.arg == "on" else 0)
            await show(c, *await stats_screen(await repo.get_reseller(reseller.id)))
        elif a == "members":
            rows = await repo.list_members(reseller.id, limit=25)
            lines = ["👥 <b>Customers</b> (latest 25)", ""]
            for mbr in rows:
                tag = f"@{esc(mbr.username)}" if mbr.username else esc(mbr.full_name or "")
                lock = " 🚫" if mbr.is_blocked else ""
                held = f" (reserved {money(mbr.held)})" if mbr.held else ""
                lines.append(f"• <code>{mbr.telegram_id}</code> {tag} — <b>{money(mbr.available)}</b>{held}{lock}")
            if not rows:
                lines.append("No customers yet. Share your bot link to get the first ones.")
            kb = InlineKeyboardBuilder()
            kb.button(text="⬅️ Admin panel", callback_data=Adm(a="home"))
            await show(c, "\n".join(lines), kb.as_markup())
        elif a == "broadcast" and reseller.status == Reseller.SUSPENDED:
            await c.answer("This bot was disabled by the platform: broadcasts are off.", show_alert=True)
            return
        elif a == "bc_send":
            text = draft
            if not text or callback_data.arg != _bc_mark(text) or reseller.status == Reseller.SUSPENDED:
                if text:
                    await state.update_data(bc_text=text)      # another draft's button: the one on screen stays
                await c.answer("This message is no longer waiting. Write it again under 📣 Broadcast.", show_alert=True)
                return
            bot = c.bot

            async def run():
                sent, failed = await selling.broadcast(bot, reseller.id, text)
                await selling.send(bot, reseller.owner_id, f"📣 Broadcast finished: delivered to <b>{sent}</b>"
                                                           + (f", {failed} could not be reached." if failed else "."))
            asyncio.create_task(run())
            await c.answer("📣 Sending… you'll get a message when it's done.", show_alert=True)
            await show(c, *await dashboard(reseller))
            return
        elif a == "deposits":
            await show(c, *await deposits_screen(reseller))
        elif a == "pay_on" and callback_data.arg in payments.PROVIDERS:
            await state.set_state(AdminForm.pay_key)
            await state.update_data(provider=callback_data.arg)
            await show(c, GATEWAY_HOWTO[callback_data.arg], _cancel_kb())
        elif a == "kasera_rate" and payments.has_key(reseller, payments.KASERA):
            await show(c, *kasera_rate_screen(reseller))
        elif a == "kasera_auto" and payments.has_key(reseller, payments.KASERA):
            await repo.update_reseller(reseller.id, kasera_auto=1)
            fresh = await repo.get_reseller(reseller.id)
            await show(c, *kasera_rate_screen(fresh, "✅ QRIS now follows today's rate."))
        elif a == "kasera_own" and payments.has_key(reseller, payments.KASERA):
            await state.set_state(AdminForm.kasera_rate)
            await show(c, KASERA_RATE_ASK, _cancel_kb())
        elif a == "kasera_extra" and payments.has_key(reseller, payments.KASERA):
            await state.set_state(AdminForm.kasera_extra)
            await show(c, "➕ <b>Percent on top of today's rate</b>\n\nSend a number from 0 to 20, for example "
                          "<code>3</code>. With 3, a dollar that costs Rp 18.000 today is Rp 18.540 in your shop. "
                          "Send <code>0</code> for the plain rate.", _cancel_kb())
        elif a == "dep_off":
            await repo.update_reseller(reseller.id, deposit_info=None)
            await show(c, *await deposits_screen(
                await repo.get_reseller(reseller.id),
                "⏸ Manual deposits are off: your customers no longer see that way to pay. Turn them on again "
                "with ✏️ Payment details."))
        elif a == "nhpay" and callback_data.arg in ("on", "off"):
            turn_on = callback_data.arg == "on"
            await payments.set_builtin(reseller.id, turn_on)
            await show(c, *await deposits_screen(
                await repo.get_reseller(reseller.id),
                "✅ Built-in crypto payments are on. Your customers can add balance by themselves now, and "
                "each payment lands in your NumberHub wallet." if turn_on else
                "⏸ Built-in crypto payments are off. An address a customer already has still works until it "
                "runs out, and a payment to it is still added."))
        elif a == "pay_off" and callback_data.arg in payments.PROVIDERS:
            await payments.disconnect(reseller.id, callback_data.arg)
            label = payments.LABEL[callback_data.arg]
            await show(c, *await deposits_screen(await repo.get_reseller(reseller.id),
                                                 f"🗑 {label} disconnected. Links already open can't be "
                                                 "confirmed automatically any more."))
        elif a == "dep_info":
            await state.set_state(AdminForm.dep_info)
            await show(c, "✏️ <b>Payment details</b>\n\nSend what your customers see when they add balance: how "
                          "to pay you, for example:\n<code>bKash: 01XXXXXXXXX\nBinance Pay ID: 123456789\n"
                          "USDT (TRC20): T...</code>\n\nUp to 1000 characters. Send <code>-</code> to turn "
                          "deposits off.", _cancel_kb())
        elif a == "dep_min":
            await state.set_state(AdminForm.dep_min)
            now = money(reseller.deposit_min) if reseller.deposit_min else "none"
            await show(c, "📉 <b>Minimum deposit</b>\n\nSend the smallest amount a customer may send, for "
                          f"example <code>1</code> or <code>0.5</code>. Send <code>-</code> for no minimum of "
                          f"your own. Now: <b>{now}</b>.\n\nAutomatic payments have their own floor whatever you "
                          "set: <b>$0.50</b> by QRIS and <b>$1.00</b> by crypto.", _cancel_kb())
        elif a == "pay_note":
            await state.set_state(AdminForm.pay_note)
            now = esc(reseller.pay_note) if getattr(reseller, "pay_note", None) else "none"
            await show(c, "📝 <b>Note for customers</b>\n\nSend a short text your customers see on the "
                          "💳 Add balance screen, under the amounts. For example how to type an amount, or "
                          "which bank apps work.\n\nPlain text, up to 300 characters. Send <code>-</code> to "
                          f"remove it.\n\nNow: {now}", _cancel_kb())
        elif a == "prices":
            await show(c, *await prices_screen(reseller, page=int(callback_data.arg) if callback_data.arg.isdigit() else 0))
        elif a == "pr_del":
            # "rule id|the page it was on": the owner stays on that page (a button from
            # before the pages carries the id alone).
            rule, _, pg = callback_data.arg.partition("|")
            ok = rule.isdigit() and await repo.delete_price_rule(reseller.id, int(rule))
            selling.forget_prices(reseller.id)
            await show(c, *await prices_screen(reseller, "🗑 Custom price removed." if ok else "",
                                               page=int(pg) if pg.isdigit() else 0))
        elif a == "pr_add":
            if await repo.count_price_rules(reseller.id) >= MAX_RULES:
                await c.answer(f"You have {MAX_RULES} custom prices, the most a bot can have. Remove one first.",
                               show_alert=True)
                return
            await state.set_state(AdminForm.price_app)
            await show(c, *await app_picker(reseller))
        elif a == "pr_app":
            await state.set_state(AdminForm.price_country)
            await state.update_data(service=callback_data.arg)
            await show(c, *await country_picker(reseller, callback_data.arg))
        elif a == "pr_pg":
            service, _, pg = callback_data.arg.partition("|")
            await state.set_state(AdminForm.price_country)
            await state.update_data(service=service)
            await show(c, *await country_picker(reseller, service, page=int(pg) if pg.isdigit() else 0))
        elif a == "pr_cty":
            service, _, country = callback_data.arg.partition("|")
            await state.set_state(AdminForm.price_value)
            await state.update_data(service=service, country=country if country != "*" else "")
            await show(c, *await value_prompt(reseller, service, country if country != "*" else ""))
        elif a == "cap":
            await state.set_state(AdminForm.profit_cap)
            now = money(reseller.max_profit) if getattr(reseller, "max_profit", None) is not None else "no cap"
            await show(c, "💰 <b>Profit cap</b>\n\nSend the most you want to earn on one number, for example "
                          "<code>0.20</code>. Expensive numbers then sell at NumberHub's price + at most that.\n"
                          f"Send <code>-</code> to remove the cap. Now: <b>{now}</b>.", _cancel_kb())
        elif a == "markup":
            # The list of products first; a product's button opens its prompt.
            kind = callback_data.arg
            if kind not in PRODUCT_LABEL:
                await show(c, *commission_screen(reseller))
            else:
                await state.set_state(AdminForm.markup)
                await state.update_data(product=kind)
                await show(c, commission_prompt(reseller, kind), _commission_cancel_kb())
        elif a in PROMPTS:
            n = len(await repo.member_chat_ids(reseller.id)) if a == "broadcast" else 0
            text = PROMPTS[a].format(max=pct(settings.max_markup_pct), now=pct(reseller.markup_pct), n=n)
            await state.set_state(getattr(AdminForm, a))
            await show(c, text, _cancel_kb())
        await c.answer()

    async def done(m: Message, reseller: Reseller, state: FSMContext, note: str):
        await state.clear()
        text, kb = await dashboard(await repo.get_reseller(reseller.id))
        await m.answer(f"{note}\n\n{text}", reply_markup=kb, disable_web_page_preview=True)

    async def _member_and_amount(m: Message, reseller: Reseller) -> tuple[Member | None, Decimal | None, str | None]:
        parts = (m.text or "").split()
        if len(parts) != 2:
            return None, None, "Send two things: the customer's ID (or @username) and the amount, e.g. <code>123456789 5</code>"
        try:
            member = await repo.find_member(reseller.id, parts[0])
        except repo.AmbiguousMember:
            return None, None, AMBIGUOUS
        if member is None:
            return None, None, "❌ No customer with that ID or username. They must open your bot once first."
        amount = parse_amount(parts[1])
        if amount is None:
            return None, None, "❌ The amount must be a positive number, e.g. <code>5</code> or <code>2.50</code>."
        return member, amount, None

    @r.message(StateFilter(AdminForm.add), F.text)
    @owner_only
    async def f_add(m: Message, reseller: Reseller, state: FSMContext, is_owner: bool = False):
        member, amount, err = await _member_and_amount(m, reseller)
        if err:
            await m.answer(err, reply_markup=_cancel_kb())
            return
        if not await repo.member_adjust(reseller.id, member.id, amount):
            await m.answer("❌ That didn't go through. Please try again.", reply_markup=_cancel_kb())
            return
        fresh = await repo.get_member(member.id)
        from app.texts import after_credit_kb
        await selling.send(m.bot, fresh.telegram_id, t(fresh.language, "msg_credited", amount=money(amount),
                                                          balance=money(fresh.available)), after_credit_kb(fresh))
        await done(m, reseller, state, f"✅ Added <b>{money(amount)}</b> to <code>{fresh.telegram_id}</code>. "
                                       f"Their balance: <b>{money(fresh.available)}</b>")

    @r.message(StateFilter(AdminForm.remove), F.text)
    @owner_only
    async def f_remove(m: Message, reseller: Reseller, state: FSMContext, is_owner: bool = False):
        member, amount, err = await _member_and_amount(m, reseller)
        if err:
            await m.answer(err, reply_markup=_cancel_kb())
            return
        if not await repo.member_adjust(reseller.id, member.id, -amount):
            fresh = await repo.get_member(member.id) or member
            if not fresh.paid_left:
                await m.answer(f"❌ They have only <b>{money(fresh.available)}</b> available "
                               "(money reserved by open orders can't be removed).", reply_markup=_cancel_kb())
                return
            can = max(Decimal("0"), min(fresh.available, fresh.by_hand))
            await m.answer(f"❌ You can remove at most <b>{money(can)}</b> from this customer. "
                           f"<b>{money(fresh.paid_left)}</b> of their balance is what they paid in themselves with "
                           "built-in payments: that is theirs to spend and can't be removed. Neither can money "
                           "reserved by open orders.", reply_markup=_cancel_kb())
            return
        fresh = await repo.get_member(member.id)
        await selling.send(m.bot, fresh.telegram_id, t(fresh.language, "msg_debited", amount=money(amount),
                                                          balance=money(fresh.available)))
        await done(m, reseller, state, f"✅ Removed <b>{money(amount)}</b> from <code>{fresh.telegram_id}</code>. "
                                       f"Their balance: <b>{money(fresh.available)}</b>")

    @r.message(StateFilter(AdminForm.markup), F.text)
    @owner_only
    async def f_markup(m: Message, reseller: Reseller, state: FSMContext, is_owner: bool = False):
        kind = (await state.get_data()).get("product")
        kind = kind if kind in PRODUCT_LABEL else "sms"
        field = selling.PRODUCTS[kind]
        if field and m.text.strip() == "-":
            # Back to the commission for numbers.
            await repo.update_reseller(reseller.id, **{field: None})
            selling.forget_prices(reseller.id)
            await state.clear()
            fresh = await repo.get_reseller(reseller.id)
            await reply(m, commission_screen(fresh, f"✅ {PRODUCT_LABEL[kind]} uses the commission for numbers again "
                                                    f"(<b>{pct(fresh.markup_pct)}%</b>). Prices update right away."))
            return
        try:
            v = Decimal(m.text.replace("%", "").replace(",", ".").strip())
        except (InvalidOperation, ValueError):
            v = Decimal("-1")
        if not v.is_finite() or v < 0 or v > settings.max_markup_pct:
            await m.answer(f"❌ Send a number from 0 to {pct(settings.max_markup_pct)}."
                           + (" Or <code>-</code> to use the commission for numbers." if field else ""),
                           reply_markup=_commission_cancel_kb())
            return
        v = v.quantize(Decimal("0.01"))
        await repo.update_reseller(reseller.id, **{field or "markup_pct": v})
        selling.forget_prices(reseller.id)
        await state.clear()
        note = (f"✅ Commission set to <b>{pct(v)}%</b>. Prices update right away." if kind == "sms" else
                f"✅ Commission for {PRODUCT_LABEL[kind]} set to <b>{pct(v)}%</b>. Prices update right away.")
        await reply(m, commission_screen(await repo.get_reseller(reseller.id), note))

    async def _mark_card(c: CallbackQuery, dep, member, status_line: str) -> None:
        """The owner's request message keeps its text and shows how it ended."""
        text = deposit_card(dep, member, status_line)
        try:
            if c.message.photo:
                await c.message.edit_caption(caption=text, reply_markup=None)
            else:
                await c.message.edit_text(text, reply_markup=None)
        except Exception:  # noqa: BLE001 — old or already edited: the outcome is already saved
            pass

    @r.callback_query(Dep.filter())
    @owner_only
    async def dep_decide(c: CallbackQuery, callback_data: Dep, reseller: Reseller, state: FSMContext,
                         is_owner: bool = False):
        dep = await repo.get_deposit(reseller.id, callback_data.id)
        if dep is None:
            await c.answer("Not found.", show_alert=True)
            return
        if dep.status != dep.PENDING:
            word = "approved" if dep.status == dep.APPROVED else "rejected"
            await c.answer(f"Deposit #{dep.number} was already {word}.", show_alert=True)
            return
        if callback_data.a == "amt":
            await state.set_state(AdminForm.dep_amount)
            await state.update_data(dep_id=dep.id)
            await c.message.answer(f"✏️ Send the amount to add for deposit #{dep.number} "
                                   f"(they said {money(dep.amount)}), for example <code>4.50</code>.",
                                   reply_markup=_cancel_kb())
            await c.answer()
            return
        done_ = await settle_deposit(c.bot, reseller, dep.id, approve=callback_data.a == "ok")
        if done_ is None:
            await c.answer("Already handled.", show_alert=True)
            return
        dep, member = done_
        line = (f"✅ Approved: +{money(dep.credited)} added." if dep.status == dep.APPROVED
                else "❌ Rejected. The customer was told.")
        await _mark_card(c, dep, member, line)
        await c.answer(line)

    @r.message(StateFilter(AdminForm.dep_amount), F.text)
    @owner_only
    async def f_dep_amount(m: Message, reseller: Reseller, state: FSMContext, is_owner: bool = False):
        amount = parse_amount(m.text)
        if amount is None:
            await m.answer("❌ Send an amount like <code>4.50</code>.", reply_markup=_cancel_kb())
            return
        dep_id = (await state.get_data()).get("dep_id")
        await state.clear()
        done_ = await settle_deposit(m.bot, reseller, int(dep_id or 0), approve=True, amount=amount)
        if done_ is None:
            await reply(m, await deposits_screen(reseller, "That deposit was already handled."))
            return
        dep, _member = done_
        await reply(m, await deposits_screen(reseller, f"✅ Deposit #{dep.number} approved: "
                                                       f"+{money(dep.credited)} added."))

    @r.message(StateFilter(AdminForm.dep_info), F.text)
    @owner_only
    async def f_dep_info(m: Message, reseller: Reseller, state: FSMContext, is_owner: bool = False):
        text = m.text.strip()
        if len(text) > 1000:
            await m.answer(f"❌ That is {len(text)} characters: the limit is 1000.", reply_markup=_cancel_kb())
            return
        await repo.update_reseller(reseller.id, deposit_info=None if text == "-" else text)
        await state.clear()
        note = "⏸ Deposits are off." if text == "-" else "✅ Payment details saved: deposits are on."
        await reply(m, await deposits_screen(await repo.get_reseller(reseller.id), note))

    @r.message(StateFilter(AdminForm.pay_key), F.text)
    @owner_only
    async def f_pay_key(m: Message, reseller: Reseller, state: FSMContext, is_owner: bool = False):
        # A payment key is a secret: off the chat at once, whatever happens next.
        try:
            await m.delete()
        except Exception:  # noqa: BLE001 — too old / no rights: the owner can delete it
            pass
        provider = (await state.get_data()).get("provider")
        raw = (m.text or "").strip()
        if provider not in payments.PROVIDERS or raw == "-":
            await state.clear()
            await reply(m, await deposits_screen(reseller))
            return
        label = payments.LABEL[provider]
        try:
            name = await payments.connect(reseller.id, provider, raw)
        except payments.PayError as exc:
            await m.answer(f"❌ {label} didn't accept that: {esc(str(exc))}\n\nSend the key again, or "
                           "<code>-</code> to cancel.", reply_markup=_cancel_kb())
            return
        except Exception:  # noqa: BLE001
            log.exception("reseller %s: connecting %s crashed", reseller.id, provider)
            await m.answer(f"❌ Couldn't reach {label} right now. Please send the key again in a minute.",
                           reply_markup=_cancel_kb())
            return
        await state.clear()
        if provider == payments.KASERA:
            # Balances are in dollars and QRIS is paid in rupiah. Unless the owner typed
            # a rate of their own before, dollars turn into rupiah at today's rate: nothing
            # to type, and nothing to type wrong (a shop saved the gateway's minimum
            # payment, 5,000, as its rate and sold $1 for Rp 5.000).
            fresh = await repo.get_reseller(reseller.id)
            if getattr(fresh, "kasera_rate", None) is None:
                await repo.update_reseller(reseller.id, kasera_auto=1)
                fresh = await repo.get_reseller(reseller.id)
            await reply(m, kasera_rate_screen(
                fresh, f"✅ <b>{label} connected</b> ({esc(name)}). Customers can now add balance by QRIS."))
            return
        await reply(m, await deposits_screen(await repo.get_reseller(reseller.id),
                                             f"✅ <b>{label} connected</b> ({esc(name)}). Customers can now "
                                             "top up by crypto, and their balance goes up by itself."))

    @r.message(StateFilter(AdminForm.kasera_rate), F.text)
    @owner_only
    async def f_kasera_rate(m: Message, reseller: Reseller, state: FSMContext, is_owner: bool = False):
        raw = (m.text or "").strip().replace(".", "").replace(",", "").replace(" ", "")
        if not raw.isdigit() or not 1000 <= int(raw) <= 100000:
            await m.answer("❌ Send the rupiah for one dollar as a number from 1000 to 100000, for example "
                           "<code>16500</code>.", reply_markup=_cancel_kb())
            return
        await repo.update_reseller(reseller.id, kasera_rate=Decimal(raw), kasera_auto=0)
        await state.clear()
        fresh = await repo.get_reseller(reseller.id)
        low, high = payments.kasera_limits(fresh)
        await reply(m, await deposits_screen(fresh, f"✅ Rate saved: Rp {int(raw):,} = $1.".replace(",", ".")
                                             + f" Customers can pay by QRIS from {money(low)} to {money(high)} at a time."))

    @r.message(StateFilter(AdminForm.kasera_extra), F.text)
    @owner_only
    async def f_kasera_extra(m: Message, reseller: Reseller, state: FSMContext, is_owner: bool = False):
        raw = (m.text or "").strip().replace("%", "").replace(",", ".")
        try:
            extra = Decimal(raw).quantize(Decimal("0.01"))
        except Exception:  # noqa: BLE001
            extra = None
        if extra is None or not extra.is_finite() or not 0 <= extra <= 20:
            await m.answer("❌ Send a number from 0 to 20, for example <code>3</code>.", reply_markup=_cancel_kb())
            return
        await repo.update_reseller(reseller.id, kasera_extra=extra, kasera_auto=1)
        await state.clear()
        await reply(m, kasera_rate_screen(await repo.get_reseller(reseller.id),
                                          f"✅ Saved: today's rate + {extra.normalize():f}%." if extra
                                          else "✅ Saved: the plain rate of the day."))

    @r.message(StateFilter(AdminForm.dep_min), F.text)
    @owner_only
    async def f_dep_min(m: Message, reseller: Reseller, state: FSMContext, is_owner: bool = False):
        raw = m.text.strip()
        value = None if raw == "-" else parse_amount(raw)
        if raw != "-" and value is None:
            await m.answer("❌ Send an amount in US dollars like <code>1</code> or <code>0.5</code>, or "
                           "<code>-</code> for no minimum.", reply_markup=_cancel_kb())
            return
        tops = [lim[1] for lim in (payments.method_limits(reseller, p) for p in payments.connected(reseller)) if lim]
        most = max(tops) if tops else payments.MAX_AMOUNT
        if value is not None and value > most:
            # A figure in another currency ("10000" meant as rupiah) would refuse every top-up.
            await m.answer(f"❌ One top-up here can be at most <b>{money(most)}</b>, so a minimum of "
                           f"{money(value)} would refuse them all. Amounts are in US dollars: send a smaller "
                           "one, or <code>-</code> for no minimum.", reply_markup=_cancel_kb())
            return
        await repo.update_reseller(reseller.id, deposit_min=value)
        await state.clear()
        note = f"✅ Minimum deposit: {money(value)}." if value else "✅ No minimum."
        await reply(m, await deposits_screen(await repo.get_reseller(reseller.id), note))

    @r.message(StateFilter(AdminForm.pay_note), F.text)
    @owner_only
    async def f_pay_note(m: Message, reseller: Reseller, state: FSMContext, is_owner: bool = False):
        raw = " ".join(m.text.split()) if m.text.strip() == "-" else m.text.strip()
        if raw != "-" and not 1 <= len(raw) <= 300:
            await m.answer("❌ Up to 300 characters, or <code>-</code> to remove the note.", reply_markup=_cancel_kb())
            return
        await repo.update_reseller(reseller.id, pay_note=None if raw == "-" else raw)
        await state.clear()
        await reply(m, await deposits_screen(await repo.get_reseller(reseller.id),
                                             "✅ Note removed." if raw == "-" else "✅ Note saved: your customers "
                                             "see it on the 💳 Add balance screen."))

    @r.message(StateFilter(AdminForm.price_app), F.text)
    @owner_only
    async def f_price_app(m: Message, reseller: Reseller, state: FSMContext, is_owner: bool = False):
        await reply(m, await app_picker(reseller, m.text[:40]))

    @r.message(StateFilter(AdminForm.price_country), F.text)
    @owner_only
    async def f_price_country(m: Message, reseller: Reseller, state: FSMContext, is_owner: bool = False):
        service = (await state.get_data()).get("service") or ""
        text, kb = await country_picker(reseller, service, m.text[:40])
        await m.answer(text, reply_markup=kb)

    @r.message(StateFilter(AdminForm.price_value), F.text)
    @owner_only
    async def f_price_value(m: Message, reseller: Reseller, state: FSMContext, is_owner: bool = False):
        from app.catalog_ui import nice_name
        data = await state.get_data()
        service, country = data.get("service") or "", data.get("country") or ""
        parsed = parse_price(m.text)
        if not service or parsed is None:
            await m.answer("❌ Send a price like <code>0.35</code> (fixed) or <code>10%</code> (commission, "
                           f"0–{pct(settings.max_markup_pct)}%).", reply_markup=_back_to_prices().as_markup())
            return
        mode, value = parsed
        if not await selling.known_service(reseller, service):
            await state.clear()
            await reply(m, await prices_screen(reseller, "❌ That app is not in the catalog any more."))
            return
        try:
            rows = await selling.countries(reseller, service)
        except selling.SellError:
            rows = []
        row = next((r for r in rows if str(r["country"]) == country), None) if country else None
        svc_name = nice_name(service, selling.service_name(await selling.services(reseller), service))
        await repo.set_price_rule(reseller.id, service, country, mode, value, service_name=svc_name,
                                  country_name=(row.get("name") if row else None))
        await state.clear()
        selling.forget_prices(reseller.id)
        fresh = await repo.get_reseller(reseller.id)
        note = "✅ Custom price saved."
        if row is not None:
            price = selling.shop_price(fresh, await selling.price_rules(reseller.id), service, country, row["ceiling"])
            note += (f" {esc(svc_name)} in {esc(row.get('name'))} now sells for <b>{money(price)}</b> "
                     f"(NumberHub {money(row['ceiling'])}, you earn {money(price - row['ceiling'])}).")
            if mode == "fixed" and value < row["ceiling"]:
                note += (f"\n⚠️ That is below NumberHub's price, so it sells at {money(row['ceiling'])} for now "
                         "and you earn nothing on it until NumberHub's price drops.")
        await reply(m, await prices_screen(fresh, note))

    @r.message(StateFilter(AdminForm.profit_cap), F.text)
    @owner_only
    async def f_profit_cap(m: Message, reseller: Reseller, state: FSMContext, is_owner: bool = False):
        raw = m.text.strip().replace("$", "").replace(",", ".")
        if raw == "-":
            cap = None
        else:
            try:
                cap = Decimal(raw)
            except (InvalidOperation, ValueError):
                cap = Decimal("-1")
            if not cap.is_finite() or cap < 0 or cap > MAX_FIXED:
                await m.answer("❌ Send an amount like <code>0.20</code>, or <code>-</code> to remove the cap.",
                               reply_markup=_cancel_kb())
                return
            cap = cap.quantize(Decimal("0.01"))
        await repo.update_reseller(reseller.id, max_profit=cap)
        selling.forget_prices(reseller.id)
        await state.clear()
        fresh = await repo.get_reseller(reseller.id)
        note = (f"✅ You now earn at most <b>{money(cap)}</b> on one number." if cap is not None
                else "✅ Profit cap removed.")
        await reply(m, await prices_screen(fresh, note))

    @r.message(StateFilter(AdminForm.welcome), F.text)
    @owner_only
    async def f_welcome(m: Message, reseller: Reseller, state: FSMContext, is_owner: bool = False):
        text = m.text.strip()
        if text == "-":
            await repo.update_reseller(reseller.id, welcome_text=None)
            await done(m, reseller, state, "✅ Back to the default welcome text.")
            return
        if len(text) > 800:
            await m.answer(f"❌ That is {len(text)} characters — the limit is 800.", reply_markup=_cancel_kb())
            return
        await repo.update_reseller(reseller.id, welcome_text=text)
        await done(m, reseller, state, "✅ Welcome text saved.")

    @r.message(StateFilter(AdminForm.support), F.text)
    @owner_only
    async def f_support(m: Message, reseller: Reseller, state: FSMContext, is_owner: bool = False):
        c = m.text.strip()
        if not (re.fullmatch(r"@[A-Za-z0-9_]{4,32}", c) or re.fullmatch(r"https://\S{4,200}", c)):
            await m.answer("❌ Send an @username (like <code>@myshop_help</code>) or a link starting with https://",
                           reply_markup=_cancel_kb())
            return
        await repo.update_reseller(reseller.id, support_contact=c)
        await done(m, reseller, state, f"✅ Support contact set to {esc(c)}.")

    @r.message(StateFilter(AdminForm.broadcast), F.text)
    @owner_only
    async def f_broadcast(m: Message, reseller: Reseller, state: FSMContext, is_owner: bool = False):
        # Nothing goes out until the owner has seen it and said so: a text typed into
        # a prompt left open went to every customer at once.
        text = m.html_text
        await state.clear()
        await state.update_data(bc_text=text)
        n = len(await repo.member_chat_ids(reseller.id))
        kb = InlineKeyboardBuilder()
        kb.button(text=f"✅ Send to {n} customers", callback_data=Adm(a="bc_send", arg=_bc_mark(text)))
        kb.button(text="✖️ Don't send", callback_data=Adm(a="home"))
        kb.adjust(1)
        await m.answer("📣 <b>Your customers will get this message:</b>\n\n" + text[:3500]
                       + "\n\n<i>Nothing has been sent yet.</i>", reply_markup=kb.as_markup())

    @r.message(StateFilter(AdminForm.block), F.text)
    @owner_only
    async def f_block(m: Message, reseller: Reseller, state: FSMContext, is_owner: bool = False):
        try:
            member = await repo.find_member(reseller.id, m.text.strip())
        except repo.AmbiguousMember:
            await m.answer(AMBIGUOUS, reply_markup=_cancel_kb())
            return
        if member is None:
            await m.answer("❌ No customer with that ID or username.", reply_markup=_cancel_kb())
            return
        if member.telegram_id == reseller.owner_id:
            await m.answer("❌ You can't block yourself.", reply_markup=_cancel_kb())
            return
        await repo.set_member_blocked(member.id, not member.is_blocked)
        word = "unblocked" if member.is_blocked else "blocked"
        await done(m, reseller, state, f"✅ <code>{member.telegram_id}</code> {word}.")
