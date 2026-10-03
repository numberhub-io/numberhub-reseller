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
from app.bots.callbacks import Adm, Nav
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
    kb.button(text="📝 Welcome text", callback_data=Adm(a="welcome"))
    kb.button(text="🆘 Support contact", callback_data=Adm(a="support"))
    kb.button(text="🚫 Block / unblock", callback_data=Adm(a="block"))
    kb.button(text="🔄 Refresh", callback_data=Adm(a="home"))
    kb.button(text="🏠 Menu", callback_data=Nav(to="menu"))
    kb.adjust(2, 2, 2, 2, 2, 1)
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


async def country_picker(reseller: Reseller, service: str, query: str = ""):
    """All countries, or one: the cheapest in-stock countries with NumberHub's price."""
    from app.catalog_ui import nice_name
    name = nice_name(service, selling.service_name(await selling.services(reseller), service))
    kb = InlineKeyboardBuilder()
    try:
        rows = await selling.countries(reseller, service)
    except selling.SellError:
        rows = []
    if query:
        q = query.strip().lower()
        rows = [r for r in rows if q in str(r.get("name") or "").lower()]
    else:
        kb.button(text="🌍 All countries", callback_data=Adm(a="pr_cty", arg=f"{service}|*"))
        rows = [r for r in rows if r.get("in_stock")] or rows
    for r in rows[:16]:
        kb.button(text=f"{r.get('emoji', '')} {r.get('name')} · NumberHub {money(r['ceiling'])}"[:60],
                  callback_data=Adm(a="pr_cty", arg=f"{service}|{r['country']}"))
    kb.button(text="✖️ Cancel", callback_data=Adm(a="prices"))
    kb.adjust(1)
    if query and not rows:
        text = f"❌ No country matches <b>{esc(query)}</b> for {esc(name)}. Send another name."
    else:
        text = (f"🏷 <b>{esc(name)}</b>: one price for all countries, or for one country?\n\n"
                "Tap one, or send a country name.")
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
