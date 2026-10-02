"""The builder bot: resellers create and manage their selling bots here.
Platform operators (ADMIN_IDS) also get an overview of every bot."""
from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app import crypto, repo, runtime
from app.config import settings
from app.models import Reseller
from app.numberhub import NumberHub, NumberHubError, dec
from app.texts import esc, money

log = logging.getLogger(__name__)
router = Router(name="builder")
MAX_BOTS_PER_OWNER = 3
TOKEN_RE = re.compile(r"^\d{5,12}:[A-Za-z0-9_-]{30,64}$")
KEY_RE = re.compile(r"^nh_[A-Za-z0-9_-]{16,120}$")   # nh_live_ + token_urlsafe(24)


class Bld(CallbackData, prefix="bd"):
    a: str
    rid: int = 0
    v: str = ""


class Create(StatesGroup):
    token = State()
    key = State()
    markup = State()
    newkey = State()


WELCOME = (
    "🤖 <b>Your own number-selling bot — live in 2 minutes</b>\n\n"
    "Sell virtual numbers for SMS codes to your own customers, under your bot's name and at your price.\n\n"
    "<b>How it works</b>\n"
    "1️⃣ Create a bot in @BotFather and send me its token.\n"
    "2️⃣ Send me your NumberHub API key. Each number your customers buy is paid from your NumberHub "
    "wallet — only when its code arrives.\n"
    "3️⃣ Set your markup. Customers top up with you, your way; you add their balance in your bot.\n\n"
    "💰 You keep the difference. No code = no charge, for you and your customer."
)
HELP = (
    "❓ <b>Help</b>\n\n"
    "<b>What do I need?</b> A Telegram bot token (from @BotFather) and a NumberHub API key "
    f"({settings.numberhub_site} → Account → API keys), with balance in your NumberHub wallet.\n\n"
    "<b>What do my customers see?</b> Your bot only: your name, your welcome text, your prices and your "
    "support contact. They pick a service and a country, get a number and receive the code in the chat.\n\n"
    "<b>How do customers pay?</b> They pay you directly (any method you like). You add the amount in your "
    "bot: /admin → ➕ Add balance.\n\n"
    "<b>What does it cost me?</b> NumberHub's price for each delivered number, from your wallet. "
    "If no code arrives, neither you nor your customer pays.\n\n"
    "<b>Your markup</b> is added on top of NumberHub's price — change it any time in /admin."
)


def home_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Create my bot", callback_data=Bld(a="create"))
    kb.button(text="🤖 My bots", callback_data=Bld(a="mine"))
    kb.button(text="❓ Help", callback_data=Bld(a="help"))
    kb.adjust(1, 2)
    return kb.as_markup()


def cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Cancel", callback_data=Bld(a="home"))
    return kb.as_markup()


async def _delete(m: Message) -> None:
    """Secrets never stay in the chat history."""
    try:
        await m.delete()
    except Exception:  # noqa: BLE001
        pass


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(WELCOME, reply_markup=home_kb(), disable_web_page_preview=True)


@router.callback_query(Bld.filter())
async def nav(c: CallbackQuery, callback_data: Bld, state: FSMContext):
    a = callback_data.a
    if a == "home":
        await state.clear()
        await c.message.edit_text(WELCOME, reply_markup=home_kb(), disable_web_page_preview=True)
    elif a == "help":
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ Create my bot", callback_data=Bld(a="create"))
        kb.button(text="⬅️ Back", callback_data=Bld(a="home"))
        kb.adjust(1)
        await c.message.edit_text(HELP, reply_markup=kb.as_markup(), disable_web_page_preview=True)
    elif a == "create":
        if len(await repo.list_resellers(owner_id=c.from_user.id)) >= MAX_BOTS_PER_OWNER:
            await c.answer(f"You can run up to {MAX_BOTS_PER_OWNER} bots.", show_alert=True)
            return
        await state.set_state(Create.token)
        await c.message.edit_text(
            "<b>Step 1 of 3 — your bot token</b>\n\n"
            "1. Open @BotFather and send /newbot\n2. Choose a name and a username for your bot\n"
            "3. Copy the token it gives you (looks like <code>123456789:AAH…</code>) and send it here.\n\n"
            "<i>I delete your message right after reading it.</i>", reply_markup=cancel_kb())
    elif a == "mine":
        await state.clear()
        await show_mine(c)
    elif a == "markup" and await state.get_state() == Create.markup.state:
        await finish_create(c.message, c.from_user.id, state, Decimal(callback_data.v), edit=True)
    elif a in ("pause", "resume", "newkey"):
        reseller = await repo.get_reseller(callback_data.rid)
        if reseller is None or reseller.owner_id != c.from_user.id:
            await c.answer("Not your bot.", show_alert=True)
            return
        if a == "pause":
            await runtime.stop(reseller.id)
            await repo.update_reseller(reseller.id, status=Reseller.DISABLED)
            await c.answer("Paused. Your customers see a short 'paused' message.")
            await show_mine(c)
        elif a == "resume":
            await repo.update_reseller(reseller.id, status=Reseller.ACTIVE)
            await runtime.start(await repo.get_reseller(reseller.id))
            await c.answer("Your bot is selling again.")
            await show_mine(c)
        else:
            await state.set_state(Create.newkey)
            await state.update_data(rid=reseller.id)
            await c.message.edit_text(f"🔑 Send the new NumberHub API key for @{esc(reseller.bot_username)}.",
                                      reply_markup=cancel_kb())
    await c.answer()


async def show_mine(c: CallbackQuery):
    rows = await repo.list_resellers(owner_id=c.from_user.id)
    kb = InlineKeyboardBuilder()
    if not rows:
        kb.button(text="➕ Create my bot", callback_data=Bld(a="create"))
        kb.button(text="⬅️ Back", callback_data=Bld(a="home"))
        kb.adjust(1)
        await c.message.edit_text("You have no bots yet.", reply_markup=kb.as_markup())
        return
    lines = ["🤖 <b>Your bots</b>", ""]
    sizes = []
    for r in rows:
        s = await repo.stats(r.id, 7)
        icon = {"active": "✅ selling", "disabled": "⏸ paused", "key_invalid": "⚠️ API key rejected"}.get(r.status, r.status)
        lines.append(f"<b>@{esc(r.bot_username)}</b> — {icon}\n👥 {s['members']} customers · 7 days: "
                     f"{s['orders']} sold · profit ≈ {money(s['sales'] - s['cost'])} · markup {r.markup_pct.normalize():f}%\n")
        kb.button(text=f"🔗 @{r.bot_username}", url=f"https://t.me/{r.bot_username}")
        if r.status == Reseller.ACTIVE:
            kb.button(text="⏸ Pause", callback_data=Bld(a="pause", rid=r.id))
        else:
            kb.button(text="▶️ Resume", callback_data=Bld(a="resume", rid=r.id))
        kb.button(text="🔑 New API key", callback_data=Bld(a="newkey", rid=r.id))
        sizes.append(3)
    kb.button(text="⬅️ Back", callback_data=Bld(a="home"))
    sizes.append(1)
    kb.adjust(*sizes)
    await c.message.edit_text("\n".join(lines) + "\n<i>Manage customers, prices and messages with /admin "
                              "inside each bot.</i>", reply_markup=kb.as_markup(), disable_web_page_preview=True)


@router.message(StateFilter(Create.token), F.text)
async def got_token(m: Message, state: FSMContext):
    token = m.text.strip()
    await _delete(m)
    if not TOKEN_RE.match(token):
        await m.answer("❌ That doesn't look like a bot token. It looks like <code>123456789:AAH…</code> — "
                       "copy it again from @BotFather.", reply_markup=cancel_kb())
        return
    probe = runtime.make_bot(token)
    try:
        me = await probe.get_me()
    except Exception:  # noqa: BLE001
        await m.answer("❌ Telegram rejected this token. Copy it again from @BotFather (or /revoke and use the new one).",
                       reply_markup=cancel_kb())
        return
    finally:
        await probe.session.close()
    if await repo.get_reseller_by_bot_id(me.id):
        await m.answer(f"❌ @{esc(me.username)} is already connected here.", reply_markup=cancel_kb())
        return
    await state.update_data(token=crypto.encrypt(token), bot_id=me.id, username=me.username, title=me.full_name)
    await state.set_state(Create.key)
    await m.answer(
        f"✅ Got it: <b>@{esc(me.username)}</b>\n\n<b>Step 2 of 3 — your NumberHub API key</b>\n\n"
        f"Open {settings.numberhub_site}/app/account/ → API keys → Create key (name it after your bot), "
        "copy the key (it starts with <code>nh_</code>) and send it here.\n\n"
        "<i>Tip: one key per bot. You can set a daily spend limit on the key for extra safety.</i>",
        reply_markup=cancel_kb(), disable_web_page_preview=True)


async def _check_key(key: str) -> tuple[dict | None, str | None]:
    if not KEY_RE.match(key):
        return None, "❌ That doesn't look like a NumberHub API key (it starts with <code>nh_</code>)."
    cli = NumberHub(key)
    try:
        return await cli.balance(), None
    except NumberHubError as exc:
        if exc.status in (401, 403):
            return None, "❌ NumberHub rejected this key. Create a new one in your account and try again."
        return None, "❌ Couldn't reach NumberHub right now. Please send the key again in a minute."
    finally:
        await cli.close()


@router.message(StateFilter(Create.key), F.text)
async def got_key(m: Message, state: FSMContext):
    key = m.text.strip()
    await _delete(m)
    bal, err = await _check_key(key)
    if err:
        await m.answer(err, reply_markup=cancel_kb())
        return
    await state.update_data(key=crypto.encrypt(key), hint=key[-4:])
    await state.set_state(Create.markup)
    kb = InlineKeyboardBuilder()
    for v in ("20", "30", "50", "100"):
        kb.button(text=f"{v}%", callback_data=Bld(a="markup", v=v))
    kb.button(text="✖️ Cancel", callback_data=Bld(a="home"))
    kb.adjust(4, 1)
    await m.answer(
        f"✅ Key works. NumberHub wallet: <b>{money(dec(bal.get('available')))}</b> available.\n\n"
        "<b>Step 3 of 3 — your markup</b>\n\nHow much do you add on top of NumberHub's price? "
        "Tap one or send a number (0–300).\n<i>Example: 30% → a $1.00 number sells for $1.30.</i>",
        reply_markup=kb.as_markup())


@router.message(StateFilter(Create.markup), F.text)
async def got_markup(m: Message, state: FSMContext):
    try:
        v = Decimal(m.text.replace("%", "").replace(",", ".").strip())
    except (InvalidOperation, ValueError):
        v = Decimal("-1")
    if not v.is_finite() or v < 0 or v > settings.max_markup_pct:
        await m.answer("❌ Send a number from 0 to 300.", reply_markup=cancel_kb())
        return
    await finish_create(m, m.from_user.id, state, v)


async def finish_create(m: Message, owner_id: int, state: FSMContext, markup: Decimal, edit: bool = False):
    data = await state.get_data()
    await state.clear()
    if not data.get("token") or not data.get("key"):
        await m.answer("Something went missing — please start again.", reply_markup=home_kb())
        return
    if await repo.get_reseller_by_bot_id(data["bot_id"]):
        await m.answer("This bot is already connected.", reply_markup=home_kb())
        return
    reseller = await repo.create_reseller(
        owner_id=owner_id, bot_token_enc=data["token"], bot_id=data["bot_id"], bot_username=data["username"],
        bot_title=data.get("title"), api_key_enc=data["key"], api_key_hint=data.get("hint"),
        markup_pct=markup.quantize(Decimal("0.01")), status=Reseller.ACTIVE)
    await runtime.start(reseller)
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🔗 Open @{reseller.bot_username}", url=f"https://t.me/{reseller.bot_username}")
    kb.button(text="🤖 My bots", callback_data=Bld(a="mine"))
    kb.adjust(1)
    text = (f"🎉 <b>@{esc(reseller.bot_username)} is live!</b>\n\n"
            f"• Share <code>t.me/{esc(reseller.bot_username)}</code> with your customers.\n"
            "• Open your bot and send /admin to add customer balance, change prices, set your support "
            "contact and welcome text, and message all customers.\n"
            "• Keep your NumberHub wallet topped up — each number is paid from it when its code arrives.")
    if edit:
        await m.edit_text(text, reply_markup=kb.as_markup(), disable_web_page_preview=True)
    else:
        await m.answer(text, reply_markup=kb.as_markup(), disable_web_page_preview=True)
    log.info("reseller %s created by %s (@%s, markup %s%%)", reseller.id, owner_id, reseller.bot_username, markup)


@router.message(StateFilter(Create.newkey), F.text)
async def got_newkey(m: Message, state: FSMContext):
    key = m.text.strip()
    await _delete(m)
    data = await state.get_data()
    reseller = await repo.get_reseller(int(data.get("rid") or 0))
    if reseller is None or reseller.owner_id != m.from_user.id:
        await state.clear()
        return
    bal, err = await _check_key(key)
    if err:
        await m.answer(err, reply_markup=cancel_kb())
        return
    await state.clear()
    await repo.update_reseller(reseller.id, api_key_enc=crypto.encrypt(key), api_key_hint=key[-4:],
                               status=Reseller.ACTIVE)
    await runtime.restart(reseller.id)
    await m.answer(f"✅ New key saved for @{esc(reseller.bot_username)} — it is selling again.",
                   reply_markup=home_kb())


# ─── platform operators ──────────────────────────────────────────────────────
def _is_operator(user_id: int) -> bool:
    return user_id in settings.admin_id_list


@router.message(Command("platform"))
async def platform(m: Message):
    if not _is_operator(m.from_user.id):
        return
    rows = await repo.list_resellers()
    lines = [f"🛠 <b>Platform</b> — {len(rows)} bots, {len(runtime.running())} running", ""]
    for r in rows:
        s = await repo.stats(r.id, 7)
        lines.append(f"#{r.id} @{esc(r.bot_username)} · owner <code>{r.owner_id}</code> · {r.status} · "
                     f"👥 {s['members']} · 7d {s['orders']} sold {money(s['sales'])}")
    lines.append("\n/disable &lt;id&gt; · /enable &lt;id&gt;")
    await m.answer("\n".join(lines))


@router.message(Command("disable"))
@router.message(Command("enable"))
async def toggle(m: Message):
    if not _is_operator(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer("Usage: /disable 12 or /enable 12")
        return
    reseller = await repo.get_reseller(int(parts[1]))
    if reseller is None:
        await m.answer("No such bot.")
        return
    if parts[0].startswith("/disable"):
        await runtime.stop(reseller.id)
        await repo.update_reseller(reseller.id, status=Reseller.DISABLED)
        await m.answer(f"⏸ #{reseller.id} @{esc(reseller.bot_username)} disabled.")
    else:
        await repo.update_reseller(reseller.id, status=Reseller.ACTIVE)
        await runtime.start(await repo.get_reseller(reseller.id))
        await m.answer(f"▶️ #{reseller.id} @{esc(reseller.bot_username)} enabled.")
