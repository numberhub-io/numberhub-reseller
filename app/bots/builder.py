"""The builder bot: resellers create and manage their selling bots here.
Platform operators (ADMIN_IDS) also get an overview of every bot."""
from __future__ import annotations

import logging
import re
import time
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
# Secrets are sent here: private chats only.
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
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
    "3️⃣ Set your commission. Customers top up with you, your way; you add their balance in your bot.\n\n"
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
    "<b>Your commission</b> is added on top of NumberHub's price — change it any time in /admin.\n\n"
    "<b>New bot token?</b> If you revoked the token in @BotFather, tap ➕ Create my bot and send the new "
    "token of the same bot: it reconnects, with its customers and balances."
)
SUSPENDED_MSG = "⛔ This bot was disabled by the platform. Contact NumberHub support."


def pct(v) -> str:
    return f"{Decimal(v).normalize():f}"


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


async def _edit(c: CallbackQuery, text: str, kb=None) -> None:
    try:
        await c.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception as exc:  # noqa: BLE001
        if "not modified" not in str(exc):
            await c.message.answer(text, reply_markup=kb, disable_web_page_preview=True)


# ─── key checks are rationed ─────────────────────────────────────────────────
# NumberHub refuses EVERY request from an IP that sent ~200 invalid keys in a
# minute, and every shop runs from this server's IP. Unrationed checks would let
# anyone black out all shops by pasting junk keys here.
_key_fails: dict[int, list[float]] = {}
_all_fails: list[float] = []
USER_FAILS, USER_WINDOW = 3, 600          # per person: 3 rejected keys per 10 minutes
ALL_FAILS, ALL_WINDOW = 20, 60            # everyone: 20 per minute


def _rationed(user_id: int) -> bool:
    now = time.monotonic()
    mine = [t for t in _key_fails.get(user_id, []) if now - t < USER_WINDOW]
    _key_fails[user_id] = mine
    _all_fails[:] = [t for t in _all_fails if now - t < ALL_WINDOW]
    return len(mine) >= USER_FAILS or len(_all_fails) >= ALL_FAILS


def _count_fail(user_id: int) -> None:
    now = time.monotonic()
    _key_fails.setdefault(user_id, []).append(now)
    _all_fails.append(now)


async def _check_key(user_id: int, key: str, open_nh_id: int | None = None) -> tuple[dict | None, str | None]:
    """Is this key usable for a shop? Checks every permission the shop needs
    that can be checked without buying (wallet, catalog, orders), and, when the
    shop has open orders, that the key is on the SAME NumberHub account."""
    if not KEY_RE.match(key):
        return None, "❌ That doesn't look like a NumberHub API key (it starts with <code>nh_</code>)."
    if _rationed(user_id):
        return None, "⏳ Too many keys tried just now. Please wait a few minutes and send it again."
    cli = NumberHub(key)
    try:
        bal = await cli.balance()
        await cli.services()
        await cli.orders(limit=1)
        if open_nh_id is not None:
            await cli.number(open_nh_id)
        return bal, None
    except NumberHubError as exc:
        if exc.status == 401:
            _count_fail(user_id)
            return None, "❌ NumberHub rejected this key. Create a new one in your account and try again."
        if exc.code == "insufficient_scope":
            need = esc(exc.data.get("required_scope") or "")
            return None, (f"❌ This key is missing a permission{f' (<code>{need}</code>)' if need else ''}. "
                          "Create a key with all permissions: catalog, orders and wallet.")
        if exc.code == "ip_not_allowed":
            return None, "❌ This key only works from certain IP addresses. Remove the IP limit or create a new key."
        if exc.status == 404 and open_nh_id is not None:
            return None, ("❌ This key is from a different NumberHub account than the bot's open orders. "
                          "Use a key from the same account, or send it again when those orders have "
                          "finished (about 20 minutes).")
        if exc.status == 403:
            _count_fail(user_id)
            return None, "❌ NumberHub rejected this key. Create a new one in your account and try again."
        return None, "❌ Couldn't reach NumberHub right now. Please send the key again in a minute."
    finally:
        await cli.close()


# ─── start / navigation ──────────────────────────────────────────────────────
@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(WELCOME, reply_markup=home_kb(), disable_web_page_preview=True)


TOKEN_STEP = ("<b>Step 1 of 3 — your bot token</b>\n\n"
              "1. Open @BotFather and send /newbot\n2. Choose a name and a username for your bot\n"
              "3. Copy the token it gives you (looks like <code>123456789:AAH…</code>) and send it here.\n\n"
              "<i>Reconnecting a bot after /revoke? Send its new token.\n"
              "I delete your message right after reading it.</i>")


@router.callback_query(Bld.filter())
async def nav(c: CallbackQuery, callback_data: Bld, state: FSMContext):
    a = callback_data.a
    if a == "home":
        await state.clear()
        await _edit(c, WELCOME, home_kb())
    elif a == "help":
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ Create my bot", callback_data=Bld(a="create"))
        kb.button(text="⬅️ Back", callback_data=Bld(a="home"))
        kb.adjust(1)
        await _edit(c, HELP, kb.as_markup())
    elif a == "create":
        await state.set_state(Create.token)
        await _edit(c, TOKEN_STEP, cancel_kb())
    elif a == "mine":
        await state.clear()
        await show_mine(c)
    elif a == "markup":
        if await state.get_state() != Create.markup.state:
            await c.answer()
            return
        v = _parse_pct(callback_data.v)
        if v is None:
            await c.answer("Send a number instead.", show_alert=True)
            return
        await finish_create(c.message, c.from_user, state, v, edit=True)
    elif a in ("pause", "resume", "newkey"):
        reseller = await repo.get_reseller(callback_data.rid)
        if reseller is None or reseller.owner_id != c.from_user.id:
            await c.answer("Not your bot.", show_alert=True)
            return
        if reseller.status == Reseller.SUSPENDED:
            await c.answer(SUSPENDED_MSG, show_alert=True)
            return
        if a == "pause":
            # The bot keeps running: customers see "paused", open orders finish.
            await repo.update_reseller(reseller.id, status=Reseller.DISABLED)
            await c.answer("Paused. Your customers see a short 'paused' message; open orders still finish.")
            await show_mine(c)
            return
        if a == "resume":
            if reseller.status == Reseller.KEY_INVALID:
                await c.answer("NumberHub rejected this bot's API key: send a new one with 🔑 New API key.",
                               show_alert=True)
                return
            if reseller.status == Reseller.TOKEN_INVALID:
                await c.answer("Telegram rejects this bot's token, or another service is using it. "
                               "Send the bot's token again (from @BotFather) to reconnect it.", show_alert=True)
                return
            await repo.update_reseller(reseller.id, status=Reseller.ACTIVE)
            await runtime.start(await repo.get_reseller(reseller.id))
            await c.answer("Your bot is selling again.")
            await show_mine(c)
            return
        await state.set_state(Create.newkey)
        await state.update_data(rid=reseller.id)
        await _edit(c, f"🔑 Send the new NumberHub API key for @{esc(reseller.bot_username)}.\n\n"
                       "<i>Use a key from the same NumberHub account while the bot has open orders.</i>",
                    cancel_kb())
    await c.answer()


STATUS_LABEL = {"active": "✅ selling", "disabled": "⏸ paused", "key_invalid": "⚠️ API key rejected — send a new key",
                "token_invalid": "⚠️ bot token rejected — send the token again",
                "suspended": "⛔ disabled by the platform"}


async def show_mine(c: CallbackQuery):
    rows = await repo.list_resellers(owner_id=c.from_user.id)
    kb = InlineKeyboardBuilder()
    if not rows:
        kb.button(text="➕ Create my bot", callback_data=Bld(a="create"))
        kb.button(text="⬅️ Back", callback_data=Bld(a="home"))
        kb.adjust(1)
        await _edit(c, "You have no bots yet.", kb.as_markup())
        return
    lines = ["🤖 <b>Your bots</b>", ""]
    sizes = []
    for r in rows:
        s = await repo.stats(r.id, 7)
        lines.append(f"<b>@{esc(r.bot_username)}</b> — {STATUS_LABEL.get(r.status, r.status)}\n👥 {s['members']} "
                     f"customers · 7 days: {s['orders']} sold · profit ≈ {money(s['sales'] - s['cost'])} · "
                     f"commission {pct(r.markup_pct)}%\n")
        kb.button(text=f"🔗 @{r.bot_username}", url=f"https://t.me/{r.bot_username}")
        n = 1
        if r.status == Reseller.ACTIVE:
            kb.button(text="⏸ Pause", callback_data=Bld(a="pause", rid=r.id))
            n += 1
        elif r.status == Reseller.DISABLED:
            kb.button(text="▶️ Resume", callback_data=Bld(a="resume", rid=r.id))
            n += 1
        if r.status != Reseller.SUSPENDED:
            kb.button(text="🔑 New API key", callback_data=Bld(a="newkey", rid=r.id))
            n += 1
        sizes.append(n)
    kb.button(text="⬅️ Back", callback_data=Bld(a="home"))
    sizes.append(1)
    kb.adjust(*sizes)
    await _edit(c, "\n".join(lines) + "\n<i>Manage customers, prices and messages with /admin "
                   "inside each bot.</i>", kb.as_markup())


# ─── create (or reconnect) a bot ─────────────────────────────────────────────
@router.message(StateFilter(Create.token), F.text)
async def got_token(m: Message, state: FSMContext):
    token = m.text.strip()
    await _delete(m)
    await _take_token(m, state, token)


async def _take_token(m: Message, state: FSMContext, token: str) -> None:
    if not TOKEN_RE.match(token):
        await m.answer("❌ That doesn't look like a bot token. It looks like <code>123456789:AAH…</code> — "
                       "copy it again from @BotFather.", reply_markup=cancel_kb())
        return
    probe = runtime.make_bot(token)
    try:
        me = await probe.get_me()
        hook = await probe.get_webhook_info()
    except Exception:  # noqa: BLE001
        await m.answer("❌ Telegram rejected this token. Copy it again from @BotFather (or /revoke and use the new one).",
                       reply_markup=cancel_kb())
        return
    finally:
        await probe.session.close()
    existing = await repo.get_reseller_by_bot_id(me.id)
    if existing is not None and existing.owner_id != m.from_user.id:
        await m.answer(f"❌ @{esc(me.username)} is already connected here by someone else.", reply_markup=cancel_kb())
        return
    if existing is not None and existing.status == Reseller.SUSPENDED:
        await m.answer(SUSPENDED_MSG, reply_markup=home_kb())
        await state.clear()
        return
    if existing is None and len(await repo.list_resellers(owner_id=m.from_user.id)) >= MAX_BOTS_PER_OWNER:
        await m.answer(f"You can run up to {MAX_BOTS_PER_OWNER} bots.", reply_markup=home_kb())
        await state.clear()
        return
    await state.update_data(token=crypto.encrypt(token), bot_id=me.id, username=me.username, title=me.full_name,
                            rid=existing.id if existing else 0)
    await state.set_state(Create.key)
    note = ("\n\n<i>This bot was connected to a webhook somewhere else; it will be switched to this "
            "platform.</i>" if getattr(hook, "url", "") else "")
    head = (f"🔁 Reconnecting <b>@{esc(me.username)}</b> (its customers and balances stay).\n\n"
            if existing else f"✅ Got it: <b>@{esc(me.username)}</b>\n\n")
    await m.answer(
        f"{head}<b>Step 2 of 3 — your NumberHub API key</b>\n\n"
        f"Open {settings.numberhub_site}/app/account/ → API keys → Create key (name it after your bot, "
        "keep every permission ticked), copy the key (it starts with <code>nh_</code>) and send it here."
        f"{note}", reply_markup=cancel_kb(), disable_web_page_preview=True)


@router.message(StateFilter(Create.key), F.text)
async def got_key(m: Message, state: FSMContext):
    key = m.text.strip()
    await _delete(m)
    data = await state.get_data()
    rid = int(data.get("rid") or 0)
    open_nh = await _an_open_order(rid) if rid else None
    bal, err = await _check_key(m.from_user.id, key, open_nh)
    if err:
        await m.answer(err, reply_markup=cancel_kb())
        return
    if rid:
        await _reconnect(m, state, rid, data, key)
        return
    await state.update_data(key=crypto.encrypt(key), hint=key[-4:])
    await state.set_state(Create.markup)
    kb = InlineKeyboardBuilder()
    presets = sorted({Decimal("20"), settings.default_markup_pct, Decimal("50"), Decimal("100")})
    for v in presets:
        star = " ⭐" if v == settings.default_markup_pct else ""
        kb.button(text=f"{pct(v)}%{star}", callback_data=Bld(a="markup", v=pct(v)))
    kb.button(text="✖️ Cancel", callback_data=Bld(a="home"))
    kb.adjust(len(presets), 1)
    await m.answer(
        f"✅ Key works. NumberHub wallet: <b>{money(dec(bal.get('available')))}</b> available.\n\n"
        "<b>Step 3 of 3 — your commission</b>\n\nHow much do you add on top of NumberHub's price? "
        f"Tap one or send a number (0–{pct(settings.max_markup_pct)}).\n"
        "<i>Example: 30% → a $1.00 number sells for $1.30 and you earn $0.30.</i>",
        reply_markup=kb.as_markup())


def _parse_pct(raw: str) -> Decimal | None:
    try:
        v = Decimal((raw or "").replace("%", "").replace(",", ".").strip())
    except (InvalidOperation, ValueError):
        return None
    if not v.is_finite() or v < 0 or v > settings.max_markup_pct:
        return None
    return v.quantize(Decimal("0.01"))


@router.message(StateFilter(Create.markup), F.text)
async def got_markup(m: Message, state: FSMContext):
    v = _parse_pct(m.text)
    if v is None:
        await m.answer(f"❌ Send a number from 0 to {pct(settings.max_markup_pct)}.", reply_markup=cancel_kb())
        return
    await finish_create(m, m.from_user, state, v)


async def finish_create(m: Message, owner, state: FSMContext, markup: Decimal, edit: bool = False):
    data = await state.get_data()
    await state.clear()
    if not data.get("token") or not data.get("key"):
        await m.answer("Something went missing — please start again.", reply_markup=home_kb())
        return
    if await repo.get_reseller_by_bot_id(data["bot_id"]):
        await m.answer("This bot is already connected.", reply_markup=home_kb())
        return
    # Customers are told to send their ID "to the seller": the owner's own
    # @username is the contact until they set another one in /admin.
    contact = f"@{owner.username}" if getattr(owner, "username", None) else None
    reseller = await repo.create_reseller(
        owner_id=owner.id, bot_token_enc=data["token"], bot_id=data["bot_id"], bot_username=data["username"],
        bot_title=data.get("title"), api_key_enc=data["key"], api_key_hint=data.get("hint"),
        markup_pct=markup, support_contact=contact, status=Reseller.ACTIVE)
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
    log.info("reseller %s created by %s (@%s, commission %s%%)", reseller.id, owner.id, reseller.bot_username, markup)


async def _an_open_order(reseller_id: int) -> int | None:
    rows = await repo.open_orders(reseller_id) + await repo.recently_received(reseller_id)
    return rows[0].nh_id if rows else None


async def _reconnect(m: Message, state: FSMContext, rid: int, data: dict, key: str) -> None:
    """Same bot, new token (after /revoke) or a lost SECRET_KEY: update the row
    in place, so the customers, balances and orders stay with the bot."""
    await state.clear()
    reseller = await repo.get_reseller(rid)
    if reseller is None or reseller.owner_id != m.from_user.id:
        return
    await repo.update_reseller(rid, bot_token_enc=data["token"], bot_username=data.get("username"),
                               bot_title=data.get("title"), api_key_enc=crypto.encrypt(key), api_key_hint=key[-4:],
                               status=Reseller.ACTIVE if reseller.status != Reseller.DISABLED else Reseller.DISABLED)
    await runtime.restart(rid)
    await m.answer(f"✅ <b>@{esc(data.get('username'))}</b> is reconnected, with its customers and balances.",
                   reply_markup=home_kb())
    log.info("reseller %s reconnected by %s", rid, m.from_user.id)


@router.message(StateFilter(Create.newkey), F.text)
async def got_newkey(m: Message, state: FSMContext):
    key = m.text.strip()
    await _delete(m)
    data = await state.get_data()
    reseller = await repo.get_reseller(int(data.get("rid") or 0))
    if reseller is None or reseller.owner_id != m.from_user.id:
        await state.clear()
        return
    if reseller.status == Reseller.SUSPENDED:
        await state.clear()
        await m.answer(SUSPENDED_MSG, reply_markup=home_kb())
        return
    bal, err = await _check_key(m.from_user.id, key, await _an_open_order(reseller.id))
    if err:
        await m.answer(err, reply_markup=cancel_kb())
        return
    await state.clear()
    status = Reseller.DISABLED if reseller.status == Reseller.DISABLED else Reseller.ACTIVE
    await repo.update_reseller(reseller.id, api_key_enc=crypto.encrypt(key), api_key_hint=key[-4:], status=status)
    await runtime.swap_key(reseller.id, key)
    await m.answer(f"✅ New key saved for @{esc(reseller.bot_username)}"
                   + (" — it is selling again." if status == Reseller.ACTIVE else " (the bot is still paused)."),
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
    # Telegram allows 4096 characters per message: send in parts.
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3800:
            await m.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await m.answer(chunk)


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
        # No new sales and the owner can't lift it; the bot keeps answering
        # "paused" and finishes the orders it has.
        await repo.update_reseller(reseller.id, status=Reseller.SUSPENDED)
        await m.answer(f"⛔ #{reseller.id} @{esc(reseller.bot_username)} disabled (open orders still finish).")
    else:
        await repo.update_reseller(reseller.id, status=Reseller.ACTIVE)
        await runtime.start(await repo.get_reseller(reseller.id))
        await m.answer(f"▶️ #{reseller.id} @{esc(reseller.bot_username)} enabled.")


# ─── anything else ───────────────────────────────────────────────────────────
@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def loose_text(m: Message, state: FSMContext):
    """Text outside a step. A pasted token starts the flow (and is deleted); a
    pasted key is deleted with a pointer; anything else gets the menu."""
    text = (m.text or "").strip()
    if TOKEN_RE.match(text):
        await _delete(m)
        await state.set_state(Create.token)
        await _take_token(m, state, text)
        return
    if KEY_RE.match(text):
        await _delete(m)
        await m.answer("🔒 I deleted that key from the chat. Tap ➕ Create my bot first (or 🤖 My bots → "
                       "🔑 New API key), then send it.", reply_markup=home_kb())
        return
    await m.answer(WELCOME, reply_markup=home_kb(), disable_web_page_preview=True)


@router.callback_query()
async def stale(c: CallbackQuery, state: FSMContext):
    """A button this bot doesn't know (an old message): never leave it spinning."""
    await state.clear()
    await c.answer("This button is from an old message.")
    await c.message.answer(WELCOME, reply_markup=home_kb(), disable_web_page_preview=True)
