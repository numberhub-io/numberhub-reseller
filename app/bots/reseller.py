"""A reseller's selling bot: what their customers see, plus the owner's admin
panel. One router per reseller (build_router), mounted on that bot's own
Dispatcher by app.runtime."""
from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app import repo, selling
from app.bots.callbacks import AZ, Adm, Buy, Cty, Lang, Nav, Noop, Ord, Svc, SvcPage
from app.catalog_ui import ANY_OTHER, LETTERS, POPULAR, by_letter, icon, letter_counts, more_popular, nice_name
from app.i18n import LANGS, resolve, t
from app.models import Member, Reseller
from app.selling import SellError
from app.texts import esc, money, order_card

log = logging.getLogger(__name__)
AZ_PAGE = 30
POPULAR_CODES = {code for code, _icon, _name in POPULAR}
CTY_PAGE = 15
STATUS_ICON = {"buying": "⏳", "pending": "🔎", "waiting": "⏳", "received": "✅", "completed": "✅",
               "canceled": "❌", "expired": "⌛", "failed": "❌"}


class AdminForm(StatesGroup):
    add = State()
    remove = State()
    markup = State()
    welcome = State()
    support = State()
    broadcast = State()
    block = State()


class Context(BaseMiddleware):
    """Loads the reseller and the member for every update; stops paused bots and
    blocked members before any handler runs."""

    def __init__(self, reseller_id: int):
        self.reseller_id = reseller_id

    async def __call__(self, handler, event, data):
        reseller = await repo.get_reseller(self.reseller_id)
        user = data.get("event_from_user")
        if reseller is None or user is None:
            return None
        is_owner = user.id == reseller.owner_id
        member = await repo.get_or_create_member(reseller.id, user.id, user.username, user.full_name,
                                                 resolve(user.language_code))
        lang = member.language or "en"
        # A paused shop still lets customers open and cancel the orders they
        # already have (their order card buttons); everything else says paused.
        own_order = isinstance(event, CallbackQuery) and (event.data or "").startswith("o:")
        if reseller.status != Reseller.ACTIVE and not is_owner and not own_order:
            await _reply(event, t(lang, "bot_paused"))
            return None
        if member.is_blocked and not is_owner:
            await _reply(event, t(lang, "blocked"))
            return None
        data.update(reseller=reseller, member=member, lang=lang, is_owner=is_owner)
        return await handler(event, data)


async def _reply(event, text: str) -> None:
    try:
        if isinstance(event, CallbackQuery):
            await event.answer(re.sub(r"<[^>]+>", "", text), show_alert=True)
        elif isinstance(event, Message):
            await event.answer(text)
    except Exception:  # noqa: BLE001
        pass


async def _show(target, text: str, kb=None) -> None:
    """Edit the message a button belongs to; for a command, send a new one."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
            return
        except Exception as exc:  # noqa: BLE001
            if "not modified" in str(exc):
                return
        await target.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    else:
        await target.answer(text, reply_markup=kb, disable_web_page_preview=True)


def support_label(reseller: Reseller, lang: str) -> str:
    return esc(reseller.support_contact) if reseller.support_contact else t(lang, "support_default")


def support_url(reseller: Reseller) -> str | None:
    c = (reseller.support_contact or "").strip()
    if re.fullmatch(r"@[A-Za-z0-9_]{4,32}", c):
        return f"https://t.me/{c[1:]}"
    if re.fullmatch(r"https://\S{4,200}", c):
        return c
    return None


# ─── screens ─────────────────────────────────────────────────────────────────
async def menu_screen(reseller: Reseller, member: Member, lang: str, is_owner: bool):
    title = esc(reseller.bot_title or reseller.bot_username or "our shop")
    head = esc(reseller.welcome_text) if reseller.welcome_text else t(lang, "welcome_default", bot=title)
    text = f"{head}\n\n{t(lang, 'menu_balance', balance=money(member.available))}"
    kb = InlineKeyboardBuilder()
    kb.button(text=t(lang, "btn_buy"), callback_data=Nav(to="buy"))
    sizes = [1]
    if member.last_service and member.last_country:
        from app.numberhub import flag
        name = await display_name(reseller, member.last_service)
        last = await repo.member_orders(member.id, limit=1)
        emoji = flag(last[0].country_iso) if last else ""
        kb.button(text=t(lang, "btn_again", service=name[:24], flag=emoji).strip(), callback_data=Nav(to="again"))
        sizes.append(1)
    kb.button(text=t(lang, "btn_orders"), callback_data=Nav(to="orders"))
    kb.button(text=t(lang, "btn_balance"), callback_data=Nav(to="balance"))
    kb.button(text=t(lang, "btn_language"), callback_data=Nav(to="lang"))
    sizes += [2, 1]
    if is_owner:
        kb.button(text="⚙️ Admin panel", callback_data=Adm(a="home"))
        sizes.append(1)
    kb.adjust(*sizes)
    return text, kb.as_markup()


async def display_name(reseller: Reseller, code: str) -> str:
    return nice_name(code, selling.service_name(await selling.services(reseller), code))


def _svc_label(reseller: Reseller, code: str, name: str) -> str:
    """'💬 WhatsApp · $0.24+' — the + marks the cheapest country's price."""
    low = selling.from_price(reseller.id, code)
    return f"{icon(code)} {name}" + (f" · {money(low)}+" if low is not None else "")


def _pairs(n: int) -> list[int]:
    return [2] * (n // 2) + ([1] if n % 2 else [])


async def services_screen(reseller: Reseller, lang: str):
    """Popular apps first, one tap away; then the next most popular; then A–Z.
    Typing a name works on every screen."""
    items = await selling.services(reseller)
    have = {s["code"] for s in items}
    kb = InlineKeyboardBuilder()
    shown = 0
    for code, _icon, name in POPULAR:
        if code in have:
            kb.button(text=_svc_label(reseller, code, name)[:40], callback_data=Svc(code=code, page=0))
            shown += 1
    sizes = _pairs(shown)
    if more_popular(items):
        kb.button(text=t(lang, "btn_more_popular"), callback_data=AZ(l="+"))
        sizes.append(1)
    kb.button(text=t(lang, "btn_all_services", n=len(items)), callback_data=AZ())
    sizes.append(1)
    if ANY_OTHER in have:
        kb.button(text=t(lang, "btn_any_other"), callback_data=Svc(code=ANY_OTHER, page=0))
        sizes.append(1)
    kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
    sizes.append(1)
    kb.adjust(*sizes)
    return t(lang, "svc_title"), kb.as_markup()


async def az_screen(reseller: Reseller, lang: str, letter: str = "", page: int = 0):
    """letter "" = the A–Z grid (also "*", the old "all services" button);
    "+" = more popular apps; "A".."Z"/"#" = that letter's apps, A to Z.
    App buttons carry no icon: 800 identical 📱 made the list a wall."""
    items = await selling.services(reseller)
    kb = InlineKeyboardBuilder()
    if letter in ("", "*"):
        counts = letter_counts(items)
        letters = [x for x in LETTERS if counts.get(x)]
        for x in letters:
            kb.button(text=x, callback_data=AZ(l=x))
        kb.button(text=t(lang, "btn_back"), callback_data=Nav(to="buy"))
        kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
        kb.adjust(*([6] * (len(letters) // 6) + ([len(letters) % 6] if len(letters) % 6 else [])), 2)
        return t(lang, "az_title", n=len(items)), kb.as_markup()
    if letter == "+":
        for s in more_popular(items):
            kb.button(text=nice_name(s["code"], s["name"])[:32], callback_data=Svc(code=s["code"], page=0))
        kb.button(text=t(lang, "btn_all_services", n=len(items)), callback_data=AZ())
        kb.button(text=t(lang, "btn_back"), callback_data=Nav(to="buy"))
        kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
        kb.adjust(*_pairs(len(more_popular(items))), 1, 2)
        return t(lang, "more_title"), kb.as_markup()
    rows = by_letter(items, letter)
    pages = max(1, (len(rows) + AZ_PAGE - 1) // AZ_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = rows[page * AZ_PAGE:(page + 1) * AZ_PAGE]
    for name, code in chunk:
        kb.button(text=name[:32], callback_data=Svc(code=code, page=0))
    sizes = _pairs(len(chunk))
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(("⬅️", AZ(l=letter, page=page - 1)))
        nav.append((f"{page + 1}/{pages}", Noop()))
        if page < pages - 1:
            nav.append(("➡️", AZ(l=letter, page=page + 1)))
        for txt, cb in nav:
            kb.button(text=txt, callback_data=cb)
        sizes.append(len(nav))
    kb.button(text=t(lang, "btn_letters"), callback_data=AZ())
    kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
    sizes.append(2)
    kb.adjust(*sizes)
    return t(lang, "az_letter", letter=letter, n=len(rows)), kb.as_markup()


def _country_label(r: dict) -> str:
    label = f"{r['emoji']} {r.get('name') or r['country']} · {money(r['member_price'])}"
    if not r.get("in_stock"):
        label += " ⏳"
    return label[:60]


async def countries_screen(reseller: Reseller, lang: str, code: str, page: int = 0):
    rows = await selling.countries(reseller, code)
    name = await display_name(reseller, code)
    kb = InlineKeyboardBuilder()
    if not rows:
        kb.button(text=t(lang, "btn_back"), callback_data=Nav(to="buy"))
        kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
        kb.adjust(2)
        return f"<b>{esc(name)}</b>\n\n{t(lang, 'cty_empty')}", kb.as_markup()
    pages = max(1, (len(rows) + CTY_PAGE - 1) // CTY_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = rows[page * CTY_PAGE:(page + 1) * CTY_PAGE]
    for r in chunk:
        kb.button(text=_country_label(r), callback_data=Cty(code=code, cc=str(r["country"])))
    nav = 0
    if pages > 1:
        if page > 0:
            kb.button(text="⬅️", callback_data=Svc(code=code, page=page - 1))
            nav += 1
        kb.button(text=f"{page + 1}/{pages}", callback_data=Noop())
        nav += 1
        if page < pages - 1:
            kb.button(text="➡️", callback_data=Svc(code=code, page=page + 1))
            nav += 1
    kb.button(text=t(lang, "btn_back"), callback_data=Nav(to="buy"))
    kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
    kb.adjust(*([1] * len(chunk)), *([nav] if nav else []), 2)
    text = t(lang, "cty_title", service=esc(name), n=len(rows))
    if any(not r.get("in_stock") for r in chunk):
        text += f"\n<i>{t(lang, 'cty_legend')}</i>"
    return text, kb.as_markup()


async def confirm_screen(reseller: Reseller, member: Member, lang: str, code: str, cc: str, note: str = ""):
    rows = await selling.countries(reseller, code)
    row = next((r for r in rows if str(r["country"]) == str(cc)), None)
    kb = InlineKeyboardBuilder()
    if row is None:
        kb.button(text=t(lang, "btn_back"), callback_data=Svc(code=code, page=0))
        kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
        kb.adjust(2)
        return t(lang, "err_sold_out"), kb.as_markup()
    name = await display_name(reseller, code)
    price = row["member_price"]
    text = t(lang, "confirm", service=esc(name), flag=row["emoji"], country=esc(row.get("name") or cc),
             price=money(price), rate="", available=money(member.available))
    if not row.get("in_stock"):
        text += t(lang, "confirm_queued")
    if note:
        text = f"{note}\n\n{text}"
    kb.button(text=t(lang, "btn_confirm", price=money(price)),
              callback_data=Buy(code=code, cc=str(cc), cents=int(price * 100)))
    sizes = [1]
    if member.available < price:
        kb.button(text=t(lang, "btn_topup"), callback_data=Nav(to="balance"))
        sizes.append(1)
    kb.button(text=t(lang, "btn_back"), callback_data=Svc(code=code, page=0))
    kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
    sizes.append(2)
    kb.adjust(*sizes)
    return text, kb.as_markup()


async def orders_screen(member: Member, lang: str):
    rows = await repo.member_orders(member.id, limit=10)
    kb = InlineKeyboardBuilder()
    if not rows:
        kb.button(text=t(lang, "btn_buy"), callback_data=Nav(to="buy"))
        kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
        kb.adjust(1)
        return t(lang, "orders_empty"), kb.as_markup()
    from app.numberhub import flag
    for o in rows:
        label = (f"{STATUS_ICON.get(o.status, '•')} {o.service_name or o.service} · {flag(o.country_iso)} "
                 f"{o.phone and ('+' + o.phone.lstrip('+')) or ''} · {money(o.member_price)}")
        kb.button(text=label[:60], callback_data=Ord(a="view", id=o.id))
    kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
    kb.adjust(1)
    return t(lang, "orders_title"), kb.as_markup()


def balance_screen(reseller: Reseller, member: Member, lang: str):
    text = t(lang, "balance", available=money(member.available), held=money(member.held),
             id=member.telegram_id, support=support_label(reseller, lang))
    kb = InlineKeyboardBuilder()
    url = support_url(reseller)
    if url:
        kb.button(text=t(lang, "btn_support"), url=url)
    kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
    kb.adjust(1)
    return text, kb.as_markup()


def error_text(exc: SellError, reseller: Reseller, member: Member, lang: str) -> str:
    if exc.reason == "no_credit":
        return t(lang, "err_no_credit", price=money(exc.price), available=money(member.available),
                 id=member.telegram_id, support=support_label(reseller, lang))
    key = {"paused": "err_paused", "sold_out": "err_sold_out", "too_many_open": "err_too_many",
           "busy": "err_busy", "processing": "err_processing", "blocked": "blocked"}.get(exc.reason, "err_failed")
    return t(lang, key)


# ─── router ──────────────────────────────────────────────────────────────────
def build_router(reseller_id: int) -> Router:
    r = Router(name=f"reseller-{reseller_id}")
    ctx = Context(reseller_id)
    r.message.outer_middleware(ctx)
    r.callback_query.outer_middleware(ctx)

    # ── navigation ──
    @r.message(CommandStart())
    @r.message(Command("menu"))
    async def start(m: Message, reseller: Reseller, member: Member, lang: str, is_owner: bool, state: FSMContext):
        await state.clear()
        await _show(m, *await menu_screen(reseller, member, lang, is_owner))

    @r.callback_query(Nav.filter())
    async def nav(c: CallbackQuery, callback_data: Nav, reseller: Reseller, member: Member, lang: str,
                  is_owner: bool, state: FSMContext):
        await state.clear()
        to = callback_data.to
        if to == "buy":
            await _show(c, *await services_screen(reseller, lang))
        elif to == "orders":
            await _show(c, *await orders_screen(member, lang))
        elif to in ("balance", "support"):   # support lives on the balance screen now
            await _show(c, *balance_screen(reseller, member, lang))
        elif to == "lang":
            kb = InlineKeyboardBuilder()
            for code, label in LANGS.items():
                kb.button(text=("✅ " if code == lang else "") + label, callback_data=Lang(code=code))
            kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
            kb.adjust(2)
            await _show(c, t(lang, "lang_title"), kb.as_markup())
        elif to == "again" and member.last_service and member.last_country:
            await _show(c, *await confirm_screen(reseller, member, lang, member.last_service, member.last_country))
        else:
            await _show(c, *await menu_screen(reseller, member, lang, is_owner))
        await c.answer()

    @r.message(Command("buy"))
    async def cmd_buy(m: Message, reseller: Reseller, lang: str):
        await _show(m, *await services_screen(reseller, lang))

    @r.message(Command("orders"))
    async def cmd_orders(m: Message, member: Member, lang: str):
        await _show(m, *await orders_screen(member, lang))

    @r.message(Command("balance"))
    async def cmd_balance(m: Message, reseller: Reseller, member: Member, lang: str):
        await _show(m, *balance_screen(reseller, member, lang))

    @r.callback_query(Lang.filter())
    async def set_lang(c: CallbackQuery, callback_data: Lang, reseller: Reseller, member: Member, is_owner: bool):
        code = callback_data.code if callback_data.code in LANGS else "en"
        await repo.set_member_language(member.id, code)
        member.language = code
        await _show(c, *await menu_screen(reseller, member, code, is_owner))
        await c.answer(t(code, "lang_set"))

    @r.callback_query(Noop.filter())
    async def noop(c: CallbackQuery):
        await c.answer()

    # ── buying ──
    @r.callback_query(SvcPage.filter())
    async def svc_page(c: CallbackQuery, reseller: Reseller, lang: str):
        await _show(c, *await services_screen(reseller, lang))
        await c.answer()

    @r.callback_query(AZ.filter())
    async def az(c: CallbackQuery, callback_data: AZ, reseller: Reseller, lang: str):
        await _show(c, *await az_screen(reseller, lang, callback_data.l, callback_data.page))
        await c.answer()

    @r.callback_query(Svc.filter())
    async def svc(c: CallbackQuery, callback_data: Svc, reseller: Reseller, lang: str, state: FSMContext):
        try:
            await _show(c, *await countries_screen(reseller, lang, callback_data.code, callback_data.page))
            # Typing now finds a COUNTRY for this service (falls back to apps).
            await state.update_data(svc=callback_data.code)
            await c.answer()
        except SellError:
            await c.answer(t(lang, "err_busy"), show_alert=True)

    @r.callback_query(Cty.filter())
    async def cty(c: CallbackQuery, callback_data: Cty, reseller: Reseller, member: Member, lang: str):
        try:
            await _show(c, *await confirm_screen(reseller, member, lang, callback_data.code, callback_data.cc))
            await c.answer()
        except SellError:
            await c.answer(t(lang, "err_busy"), show_alert=True)

    @r.callback_query(Buy.filter())
    async def buy(c: CallbackQuery, callback_data: Buy, reseller: Reseller, member: Member, lang: str):
        await c.answer(t(lang, "buying"))
        shown = Decimal(callback_data.cents) / 100
        try:
            order = await selling.buy(reseller, member, callback_data.code, callback_data.cc, shown)
        except SellError as exc:
            fresh = await repo.get_member(member.id) or member
            if exc.reason == "price_changed":
                note = t(lang, "err_price_changed", price=money(exc.price))
                await _show(c, *await confirm_screen(reseller, fresh, lang, callback_data.code, callback_data.cc, note))
                return
            kb = InlineKeyboardBuilder()
            if exc.reason == "no_credit" and support_url(reseller):
                kb.button(text=t(lang, "btn_support"), url=support_url(reseller))
            if exc.reason == "no_credit":
                # Back to the same number, ready to buy once the balance is added.
                kb.button(text=t(lang, "btn_back"), callback_data=Cty(code=callback_data.code, cc=callback_data.cc))
            if exc.reason in ("sold_out",):
                kb.button(text=t(lang, "btn_back"), callback_data=Svc(code=callback_data.code, page=0))
            if exc.reason == "processing":
                kb.button(text=t(lang, "btn_orders"), callback_data=Nav(to="orders"))
            kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
            kb.adjust(1)
            await _show(c, error_text(exc, reseller, fresh, lang), kb.as_markup())
            return
        except Exception:  # noqa: BLE001
            log.exception("buy crashed")
            await _show(c, t(lang, "err_failed"))
            return
        await repo.set_member_last(member.id, callback_data.code, callback_data.cc)
        text, kb = order_card(member, order)
        await _show(c, text, kb)
        await repo.set_card(order.id, c.message.chat.id, c.message.message_id)

    # ── orders ──
    @r.callback_query(Ord.filter())
    async def ord_(c: CallbackQuery, callback_data: Ord, reseller: Reseller, member: Member, lang: str):
        order = await repo.get_order(callback_data.id)
        if order is None or order.member_id != member.id:
            await c.answer(t(lang, "toast_closed"), show_alert=True)
            return
        if callback_data.a == "cancel":
            ok, why, secs = await selling.cancel(reseller, member, order.id)
            order = await repo.get_order(order.id)
            if ok:
                await c.answer(t(lang, "toast_cancelled", price=money(order.member_price)), show_alert=True)
            elif why == "locked":
                await c.answer(t(lang, "toast_locked", s=secs), show_alert=True)
            elif why == "code_received":
                await c.answer(t(lang, "toast_code_first"), show_alert=True)
            elif why == "busy":
                await c.answer(t(lang, "err_busy"), show_alert=True)
            else:
                await c.answer(t(lang, "toast_closed"), show_alert=True)
            text, kb = order_card(member, order)
            await _show(c, text, kb)
            await repo.set_card(order.id, c.message.chat.id, c.message.message_id)
            return
        # view: show the card here and make it the one that refreshes
        text, kb = order_card(member, order)
        await _show(c, text, kb)
        await repo.set_card(order.id, c.message.chat.id, c.message.message_id)
        await c.answer()

    # ── owner admin panel ──
    from app.bots import admin
    admin.register(r)

    # ── free text = service search (last, after the admin input states) ──
    @r.message(StateFilter(None), F.text, ~F.text.startswith("/"))
    async def search(m: Message, reseller: Reseller, lang: str, state: FSMContext):
        raw = m.text.strip()[:40]
        q, ql = esc(raw), raw.lower()
        # On a country list: the text is most likely a country for that app.
        svc_code = (await state.get_data()).get("svc")
        if svc_code:
            try:
                rows = await selling.countries(reseller, svc_code)
            except SellError:
                rows = []
            hits = [row for row in rows if ql in (row.get("name") or "").lower()]
            if hits:
                kb = InlineKeyboardBuilder()
                for row in hits[:CTY_PAGE]:
                    kb.button(text=_country_label(row), callback_data=Cty(code=svc_code, cc=str(row["country"])))
                kb.button(text=t(lang, "btn_back"), callback_data=Svc(code=svc_code, page=0))
                kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
                kb.adjust(*([1] * min(len(hits), CTY_PAGE)), 2)
                name = await display_name(reseller, svc_code)
                await m.answer(t(lang, "cty_results", service=esc(name), q=q), reply_markup=kb.as_markup())
                return
        # Apps: by our clean name (ChatGPT, X (Twitter)…) and the catalog's own.
        items = await selling.services(reseller)
        scored = []
        for s in items:
            nice = nice_name(s["code"], s["name"]).lower()
            api = (s["name"] or "").lower()
            if nice.startswith(ql) or api.startswith(ql) or s["code"] == ql:
                scored.append((0, s))
            elif ql in nice or ql in api:
                scored.append((1, s))
        found = [s for _, s in sorted(scored, key=lambda x: x[0])][:12]
        kb = InlineKeyboardBuilder()
        for s in found:
            name = nice_name(s["code"], s["name"])
            label = f"{icon(s['code'])} {name}" if s["code"] in POPULAR_CODES else name
            kb.button(text=label[:36], callback_data=Svc(code=s["code"], page=0))
        kb.button(text=t(lang, "btn_all_services", n=len(items)), callback_data=AZ())
        kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
        kb.adjust(*([2] * (len(found) // 2) + ([1] if len(found) % 2 else [])), 1, 1)
        await m.answer(t(lang, "svc_results", q=q) if found else t(lang, "svc_none", q=q),
                       reply_markup=kb.as_markup())

    # ── any button this bot no longer knows (an old message, a bot that used to
    #    run another router): answer it so it never spins, and open the menu ──
    @r.callback_query()
    async def stale(c: CallbackQuery, reseller: Reseller, member: Member, lang: str, is_owner: bool,
                    state: FSMContext):
        await state.clear()
        await c.answer(t(lang, "toast_stale"))
        await _show(c, *await menu_screen(reseller, member, lang, is_owner))

    return r


def parse_amount(raw: str) -> Decimal | None:
    try:
        v = Decimal(raw.replace("$", "").replace(",", ".").strip())
    except (InvalidOperation, ValueError):
        return None
    if not v.is_finite() or v <= 0 or v > 100000:
        return None
    return v.quantize(Decimal("0.01"))
