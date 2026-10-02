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

    pct = f"{reseller.markup_pct.normalize():f}"
    # A worked example from a real route when its price is known, else $1.00.
    cust = selling.from_price(reseller.id, "wa")
    if cust is not None:
        nh = selling.original_ceiling(cust, reseller.markup_pct)
        example = (f"<i>e.g. 💬 WhatsApp: NumberHub {money(nh)} → your customers {money(cust)} "
                   f"→ you earn {money(cust - nh)}</i>")
    else:
        cust = selling.member_price(Decimal("1.00"), reseller.markup_pct)
        example = f"<i>e.g. a $1.00 number sells for {money(cust)} → you earn {money(cust - Decimal('1.00'))}</i>"

    text = "\n".join([
        f"⚙️ <b>Admin panel</b> · @{esc(reseller.bot_username)}",
        "",
        f"👥 Customers: <b>{day['members']}</b>",
        line("Today", day), line("7 days", week), line("30 days", month),
        "",
        f"💲 Your commission: <b>{pct}%</b> on top of NumberHub's price",
        f"     {example}",
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
    kb.button(text="📝 Welcome text", callback_data=Adm(a="welcome"))
    kb.button(text="🆘 Support contact", callback_data=Adm(a="support"))
    kb.button(text="🚫 Block / unblock", callback_data=Adm(a="block"))
    kb.button(text="🔄 Refresh", callback_data=Adm(a="home"))
    kb.button(text="🏠 Menu", callback_data=Nav(to="menu"))
    kb.adjust(2, 2, 2, 2, 2)
    return text, kb.as_markup()


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
        elif a in PROMPTS:
            n = len(await repo.member_chat_ids(reseller.id)) if a == "broadcast" else 0
            text = PROMPTS[a].format(max=settings.max_markup_pct.normalize(), now=reseller.markup_pct.normalize(), n=n)
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
        member = await repo.find_member(reseller.id, parts[0])
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
        await repo.member_adjust(reseller.id, member.id, amount)
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
            await m.answer(f"❌ Send a number from 0 to {settings.max_markup_pct.normalize()}.", reply_markup=_cancel_kb())
            return
        v = v.quantize(Decimal("0.01"))
        await repo.update_reseller(reseller.id, markup_pct=v)
        selling.invalidate_prices(reseller.id)
        await done(m, reseller, state, f"✅ Commission set to <b>{v.normalize():f}%</b>. Prices update right away.")

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
        member = await repo.find_member(reseller.id, m.text.strip())
        if member is None:
            await m.answer("❌ No customer with that ID or username.", reply_markup=_cancel_kb())
            return
        if member.telegram_id == reseller.owner_id:
            await m.answer("❌ You can't block yourself.", reply_markup=_cancel_kb())
            return
        await repo.set_member_blocked(member.id, not member.is_blocked)
        word = "unblocked" if member.is_blocked else "blocked"
        await done(m, reseller, state, f"✅ <code>{member.telegram_id}</code> {word}.")
