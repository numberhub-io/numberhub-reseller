"""The reseller's admin panel inside their own bot (owner only, English)."""
from __future__ import annotations

import asyncio
import inspect
import logging
import re
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app import repo, selling
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
    n_rules = await repo.count_price_rules(reseller.id)
    extras = []
    if getattr(reseller, "max_profit", None) is not None:
        extras.append(f"💰 Most you earn on one number: <b>{money(reseller.max_profit)}</b>")
    if n_rules:
        extras.append(f"🏷 Custom prices: <b>{n_rules}</b>")
    waiting = len(await repo.pending_deposits(reseller.id, limit=100))
    if waiting:
        extras.append(f"💳 Deposits waiting for you: <b>{waiting}</b>")

    text = "\n".join([
        f"⚙️ <b>Admin panel</b> · @{esc(reseller.bot_username)}",
        "",
        f"👥 Customers: <b>{day['members']}</b>",
        line("Today", day), line("7 days", week), line("30 days", month),
        "",
        f"💲 Your commission: <b>{commission}%</b> on top of NumberHub's price",
        f"     {example}",
        *extras,
        f"🏦 NumberHub wallet: {wallet}",
        f"🔗 Share your bot: <code>t.me/{esc(reseller.bot_username)}</code>",
        "",
        "<i>Customers top up with you, your way. You add the amount here, and each number "
        "is paid from your NumberHub wallet only when its code arrives.</i>",
    ])
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Add balance", callback_data=Adm(a="add"))
    kb.button(text="➖ Remove balance", callback_data=Adm(a="remove"))
    kb.button(text="👥 Customers", callback_data=Adm(a="members"))
    kb.button(text="📣 Broadcast", callback_data=Adm(a="broadcast"))
    kb.button(text="💲 Commission", callback_data=Adm(a="markup"))
    kb.button(text="🏷 Custom prices", callback_data=Adm(a="prices"))
    kb.button(text="💳 Deposits" + (f" ({waiting})" if waiting else ""), callback_data=Adm(a="deposits"))
    kb.button(text="📝 Welcome text", callback_data=Adm(a="welcome"))
    kb.button(text="🆘 Support contact", callback_data=Adm(a="support"))
    kb.button(text="🚫 Block / unblock", callback_data=Adm(a="block"))
    kb.button(text="🔄 Refresh", callback_data=Adm(a="home"))
    kb.button(text="🏠 Menu", callback_data=Nav(to="menu"))
    kb.adjust(2, 2, 2, 2, 2, 2)
    return text, kb.as_markup()


MAX_RULES = 200
MAX_FIXED = Decimal("1000")


def rule_text(r) -> str:
    where = esc(r.country_name or r.country) if r.country else "all countries"
    what = f"{money(r.value)} fixed" if r.mode == "fixed" else f"{pct(r.value)}% commission"
    return f"{esc(r.service_name or r.service)} · {where}: <b>{what}</b>"


async def prices_screen(reseller: Reseller, note: str = ""):
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
    if rules:
        for i, r in enumerate(rules, 1):
            lines.append(f"{i}. {rule_text(r)}")
            kb.button(text=f"🗑 {i}", callback_data=Adm(a="pr_del", arg=str(r.id)))
    else:
        lines.append("No custom prices yet.")
    n = len(rules)
    kb.button(text="➕ Add a custom price", callback_data=Adm(a="pr_add"))
    kb.button(text="💰 Profit cap", callback_data=Adm(a="cap"))
    kb.button(text="⬅️ Admin panel", callback_data=Adm(a="home"))
    kb.adjust(*([5] * (n // 5) + ([n % 5] if n % 5 else [])), 1, 1, 1)
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
    except Exception:  # noqa: BLE001 — the request is saved: it also waits in ⚙️ Admin → 💳 Deposits
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
        await selling.send(bot, member.telegram_id, msg)
    return dep, member


async def deposits_screen(reseller: Reseller, note: str = ""):
    rows = await repo.pending_deposits(reseller.id, limit=10)
    on = bool(reseller.deposit_info)
    lines = ([note, ""] if note else []) + [
        "💳 <b>Deposits</b>",
        "",
        ("✅ On. Customers tap 💳 Add balance, pay you your way, then send the amount and a "
         "transaction ID or screenshot. You approve here, and their balance goes up at once."
         if on else "⏸ Off. Customers send their ID to your support contact and you add balance by hand. "
                    "Turn deposits on by setting your payment details."),
    ]
    if on:
        lines += ["", "<b>What customers see:</b>", esc(reseller.deposit_info)]
        if reseller.deposit_min:
            lines.append(f"Minimum: <b>{money(reseller.deposit_min)}</b>")
    lines += ["", f"<b>Waiting for you:</b> {len(rows)}" if rows else "Nothing waiting."]
    kb = InlineKeyboardBuilder()
    sizes = []
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
    kb.button(text="⬅️ Admin panel", callback_data=Adm(a="home"))
    kb.adjust(*sizes, 2, 1)
    return "\n".join(lines), kb.as_markup()


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
        await state.clear()
        if a == "home":
            await show(c, *await dashboard(reseller))
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
        elif a == "deposits":
            await show(c, *await deposits_screen(reseller))
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
                          f"example <code>1</code>. Send <code>-</code> for no minimum. Now: <b>{now}</b>.",
                       _cancel_kb())
        elif a == "prices":
            await show(c, *await prices_screen(reseller))
        elif a == "pr_del":
            ok = callback_data.arg.isdigit() and await repo.delete_price_rule(reseller.id, int(callback_data.arg))
            selling.forget_prices(reseller.id)
            await show(c, *await prices_screen(reseller, "🗑 Custom price removed." if ok else ""))
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
        await selling.send(m.bot, fresh.telegram_id, t(fresh.language, "msg_credited", amount=money(amount),
                                                          balance=money(fresh.available)))
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
            await m.answer(f"❌ They have only <b>{money(member.available)}</b> available "
                           "(money reserved by open orders can't be removed).", reply_markup=_cancel_kb())
            return
        fresh = await repo.get_member(member.id)
        await selling.send(m.bot, fresh.telegram_id, t(fresh.language, "msg_debited", amount=money(amount),
                                                          balance=money(fresh.available)))
        await done(m, reseller, state, f"✅ Removed <b>{money(amount)}</b> from <code>{fresh.telegram_id}</code>. "
                                       f"Their balance: <b>{money(fresh.available)}</b>")

    @r.message(StateFilter(AdminForm.markup), F.text)
    @owner_only
    async def f_markup(m: Message, reseller: Reseller, state: FSMContext, is_owner: bool = False):
        try:
            v = Decimal(m.text.replace("%", "").replace(",", ".").strip())
        except (InvalidOperation, ValueError):
            v = Decimal("-1")
        if not v.is_finite() or v < 0 or v > settings.max_markup_pct:
            await m.answer(f"❌ Send a number from 0 to {pct(settings.max_markup_pct)}.", reply_markup=_cancel_kb())
            return
        v = v.quantize(Decimal("0.01"))
        await repo.update_reseller(reseller.id, markup_pct=v)
        selling.forget_prices(reseller.id)
        await done(m, reseller, state, f"✅ Commission set to <b>{pct(v)}%</b>. Prices update right away.")

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

    @r.message(StateFilter(AdminForm.dep_min), F.text)
    @owner_only
    async def f_dep_min(m: Message, reseller: Reseller, state: FSMContext, is_owner: bool = False):
        raw = m.text.strip()
        value = None if raw == "-" else parse_amount(raw)
        if raw != "-" and value is None:
            await m.answer("❌ Send an amount like <code>1</code>, or <code>-</code> for no minimum.",
                           reply_markup=_cancel_kb())
            return
        await repo.update_reseller(reseller.id, deposit_min=value)
        await state.clear()
        note = f"✅ Minimum deposit: {money(value)}." if value else "✅ No minimum."
        await reply(m, await deposits_screen(await repo.get_reseller(reseller.id), note))

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
        await state.clear()
        text = m.html_text
        bot = m.bot

        async def run():
            sent, failed = await selling.broadcast(bot, reseller.id, text)
            await selling.send(bot, reseller.owner_id, f"📣 Broadcast finished: delivered to <b>{sent}</b>"
                                                       + (f", {failed} could not be reached." if failed else "."))
        asyncio.create_task(run())
        await m.answer("📣 Sending… you'll get a message when it's done.")

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
