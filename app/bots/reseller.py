"""A reseller's selling bot: what their customers see, plus the owner's admin
panel. One router per reseller (build_router), mounted on that bot's own
Dispatcher by app.runtime."""
from __future__ import annotations

import datetime as dt
import logging
import re
from decimal import Decimal, InvalidOperation

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app import payments, repo, selling
from app.bots.callbacks import AZ, Adm, Buy, Cty, Em, Lang, Nav, Noop, Ord, Pay, Svc, SvcPage
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
    price_app = State()        # custom price: which app (typed name)
    price_country = State()    # ...which country (typed name), app in state data
    price_value = State()      # ...the price, app + country in state data
    profit_cap = State()
    dep_info = State()         # the owner's payment details for deposits
    dep_min = State()
    dep_amount = State()       # approve a deposit with another amount (deposit id in state data)
    pay_key = State()          # an OxaPay key / @CryptoBot token / KaseraPay key (provider in state data)
    kasera_rate = State()      # rupiah for one dollar in this shop
    kasera_extra = State()     # percent on top of today's rate


class DepositForm(StatesGroup):
    amount = State()           # the customer: how much they paid
    proof = State()            # ...and the transaction ID or a screenshot


class PayForm(StatesGroup):
    amount = State()           # the customer types an amount for an automatic payment link


class EmailForm(StatesGroup):
    site = State()             # email codes: the customer may type the name of a site


class Context(BaseMiddleware):
    """Loads the reseller and the member for every update; stops paused bots and
    blocked members before any handler runs."""

    def __init__(self, reseller_id: int):
        self.reseller_id = reseller_id

    async def __call__(self, handler, event, data):
        # Private chats only: an order card shown in a group would hand the
        # member's number and SMS code to everyone in it.
        chat = event.chat if isinstance(event, Message) else (event.message.chat if event.message else None)
        if chat is None or chat.type != "private":
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer()
                except Exception:  # noqa: BLE001
                    pass
            return None
        reseller = await repo.get_reseller(self.reseller_id)
        user = data.get("event_from_user")
        if reseller is None or user is None:
            return None
        is_owner = user.id == reseller.owner_id
        if is_owner:
            await _leave_admin_form(event, data.get("state"))
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


async def _leave_admin_form(event, state: FSMContext | None) -> None:
    """An admin prompt (broadcast, welcome text, …) ends as soon as the owner does
    anything else: a command, or a button outside the admin panel. Otherwise a
    later search ("whatsapp") would go out to every customer as a broadcast."""
    if state is None:
        return
    current = await state.get_state()
    if not current or not current.startswith("AdminForm:"):
        return
    if isinstance(event, Message):
        leaving = (event.text or "").startswith("/")
    else:
        leaving = not (event.data or "").startswith("a:")
    if leaving:
        await state.set_state(None)


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
    if selling.email_on(reseller) and await selling.email_sites(reseller):
        kb.button(text=t(lang, "btn_email"), callback_data=Em(a="home"))
        sizes.append(1)
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
        kb.button(text=t(lang, "btn_back"), callback_data=Nav(to="apps"))
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
    kb.button(text=t(lang, "btn_back"), callback_data=Nav(to="apps"))
    kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
    kb.adjust(*([1] * len(chunk)), *([nav] if nav else []), 2)
    text = t(lang, "cty_title", service=esc(name), n=len(rows))
    if any(not r.get("in_stock") for r in chunk):
        text += f"\n<i>{t(lang, 'cty_legend')}</i>"
    return text, kb.as_markup()


async def confirm_screen(reseller: Reseller, member: Member, lang: str, code: str, cc: str, note: str = "",
                         back_page: int = 0):
    rows = await selling.countries(reseller, code)
    row = next((r for r in rows if str(r["country"]) == str(cc)), None)
    kb = InlineKeyboardBuilder()
    if row is None:
        kb.button(text=t(lang, "btn_back"), callback_data=Svc(code=code, page=back_page))
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
    kb.button(text=t(lang, "btn_back"), callback_data=Svc(code=code, page=back_page))
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
        if getattr(o, "kind", "sms") == "email":
            label = f"{STATUS_ICON.get(o.status, '•')} 📧 {o.service_name or o.service} · {money(o.member_price)}"
        else:
            label = (f"{STATUS_ICON.get(o.status, '•')} {o.service_name or o.service} · {flag(o.country_iso)} "
                     f"{o.phone and ('+' + o.phone.lstrip('+')) or ''} · {money(o.member_price)}")
        kb.button(text=label[:60], callback_data=Ord(a="view", id=o.id))
    kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
    kb.adjust(1)
    return t(lang, "orders_title"), kb.as_markup()


def balance_screen(reseller: Reseller, member: Member, lang: str):
    auto = bool(payments.connected(reseller))
    if auto:
        text = t(lang, "balance_auto", available=money(member.available), held=money(member.held),
                 id=member.telegram_id)
    else:
        text = t(lang, "balance", available=money(member.available), held=money(member.held),
                 id=member.telegram_id, support=support_label(reseller, lang))
    kb = InlineKeyboardBuilder()
    if auto or reseller.deposit_info:
        # Automatic payment links, or deposits the owner approves.
        kb.button(text=t(lang, "btn_topup"), callback_data=Nav(to="deposit"))
    url = support_url(reseller)
    if url:
        kb.button(text=t(lang, "btn_support"), url=url)
    kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
    kb.adjust(1)
    return text, kb.as_markup()


DEPOSIT_MAX = Decimal("10000")


def _cancel_member(lang: str):
    kb = InlineKeyboardBuilder()
    kb.button(text=t(lang, "btn_cancel"), callback_data=Nav(to="menu"))
    return kb.as_markup()


async def deposit_start(reseller: Reseller, member: Member, lang: str) -> tuple[str, object, bool]:
    """(text, keyboard, asking) for the customer's first deposit step. `asking`
    False = a refusal (one waiting already, too many today, deposits off)."""
    if not reseller.deposit_info:
        return balance_screen(reseller, member, lang) + (False,)
    waiting = await repo.pending_deposit(member.id)
    if waiting is not None:
        return t(lang, "dep_pending", n=waiting.number, amount=money(waiting.amount)), _cancel_member(lang), False
    if await repo.deposits_today(member.id) >= repo.DEPOSITS_PER_DAY:
        return t(lang, "dep_too_many"), _cancel_member(lang), False
    text = t(lang, "dep_how", info=esc(reseller.deposit_info))
    if reseller.deposit_min:
        text += "\n" + t(lang, "dep_min", min=money(reseller.deposit_min))
    return text, _cancel_member(lang), True


GATEWAY = {"ox": payments.OXAPAY, "cb": payments.CRYPTOBOT, "kq": payments.KASERA}
SHORT = {v: k for k, v in GATEWAY.items()}
# Built-in payments. Buttons made before every coin was offered carried these two
# codes; they still work. Coin and network names are the same in every language:
# they are what a wallet or an exchange shows.
NETWORK = {"nb": "usdt_bsc", "nt": "usdt_tron"}
COIN_ICON = {"USDT": "💵", "USDC": "💵", "DAI": "💵", "BTC": "₿"}


def rupiah(n) -> str:
    return "Rp " + f"{int(n):,}".replace(",", ".")


async def send_qris(target, p, lang: str) -> None:
    """A QRIS payment: the QR as a picture, the rupiah to pay and the dollars it adds."""
    d = payments.details(p)
    until = p.expires_at if p.expires_at is None or p.expires_at.tzinfo else p.expires_at.replace(tzinfo=dt.timezone.utc)
    left = int((until - dt.datetime.now(dt.timezone.utc)).total_seconds() // 60) if until else payments.INVOICE_MINUTES
    kb = InlineKeyboardBuilder()
    kb.button(text=t(lang, "btn_pay_check"), callback_data=Pay(a="chk", id=p.id))
    kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
    kb.adjust(1)
    msg = target.message if isinstance(target, CallbackQuery) else target
    await msg.answer_photo(BufferedInputFile(payments.qr_png(d.get("qr", "")), filename="qris.png"),
                           caption=t(lang, "pay_qris", idr=rupiah(d.get("idr", 0)), amount=money(p.amount),
                                     minutes=max(1, left)), reply_markup=kb.as_markup())


def address_screen(p, lang: str):
    """A built-in payment: where to send, exactly how much, on which network, until when."""
    d = payments.details(p)
    until = p.expires_at if p.expires_at is None or p.expires_at.tzinfo else p.expires_at.replace(tzinfo=dt.timezone.utc)
    left = int((until - dt.datetime.now(dt.timezone.utc)).total_seconds() // 60) if until else payments.BUILTIN_MINUTES
    kb = InlineKeyboardBuilder()
    kb.button(text=t(lang, "btn_pay_check"), callback_data=Pay(a="chk", id=p.id))
    kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
    kb.adjust(1)
    return t(lang, "pay_address", amount=money(p.amount), pay_amount=esc(d.get("pay_amount", "")),
             coin=esc(d.get("pay_currency") or "USDT"),
             network=esc(d.get("network", "")), address=esc(d.get("address", "")),
             minutes=max(1, left)), kb.as_markup()


def pay_limits(reseller: Reseller) -> tuple[Decimal, Decimal]:
    low = Decimal(reseller.deposit_min) if reseller.deposit_min else Decimal("1")
    return max(low, Decimal("1")), payments.MAX_AMOUNT


async def pay_amount_screen(reseller: Reseller, member: Member, lang: str):
    """Automatic top-up, step 1: the amount (and the link still open, if any)."""
    low, high = pay_limits(reseller)
    text = t(lang, "pay_pick_amount")
    if reseller.deposit_min:
        text += "\n" + t(lang, "dep_min", min=money(low))
    kb = InlineKeyboardBuilder()
    sizes = []
    open_ = await repo.open_payments(member.id)
    if open_ and open_[0].pay_url:
        text += "\n\n" + t(lang, "pay_open_hint", amount=money(open_[0].amount))
        kb.button(text=t(lang, "btn_pay_now", amount=money(open_[0].amount)), url=open_[0].pay_url)
        kb.button(text=t(lang, "btn_pay_check"), callback_data=Pay(a="chk", id=open_[0].id))
        sizes += [1, 1]
    elif open_ and (payments.details(open_[0]).get("address") or payments.details(open_[0]).get("qr")):
        # A built-in payment has an address instead of a link: one tap shows it again.
        qr = bool(payments.details(open_[0]).get("qr"))      # a QRIS payment has a QR, not an address
        text += "\n\n" + t(lang, "pay_open_hint_qr" if qr else "pay_open_hint_address", amount=money(open_[0].amount))
        kb.button(text=t(lang, "btn_pay_show_qr" if qr else "btn_pay_show", amount=money(open_[0].amount)),
                  callback_data=Pay(a="show", id=open_[0].id))
        kb.button(text=t(lang, "btn_pay_check"), callback_data=Pay(a="chk", id=open_[0].id))
        sizes += [1, 1]
    presets = [p for p in payments.PRESETS if low <= p <= high]
    for p in presets:
        kb.button(text=money(p), callback_data=Pay(a="amt", c=int(p * 100)))
    sizes += [2] * (len(presets) // 2) + ([1] if len(presets) % 2 else [])
    kb.button(text=t(lang, "btn_amount_other"), callback_data=Pay(a="other"))
    sizes.append(1)
    if reseller.deposit_info:
        kb.button(text=t(lang, "btn_pay_other_way"), callback_data=Nav(to="deposit_manual"))
        sizes.append(1)
    kb.button(text=t(lang, "btn_cancel"), callback_data=Nav(to="menu"))
    sizes.append(1)
    kb.adjust(*sizes)
    return text, kb.as_markup()


# ─── email codes ─────────────────────────────────────────────────────────────
EMAIL_SITES_SHOWN = 16
EMAIL_DOMAINS_SHOWN = 10
DELIVERY_MARK = {"high": "✅", "low": "⚠️"}


def email_home_screen(lang: str, sites: list[dict], note: str = ""):
    """Which site sends the code: the most asked for as buttons, or type a name."""
    kb = InlineKeyboardBuilder()
    for i, s in enumerate(sites[:EMAIL_SITES_SHOWN]):
        kb.button(text=str(s["name"])[:28], callback_data=Em(a="site", i=i))
    n = len(sites[:EMAIL_SITES_SHOWN])
    kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
    kb.adjust(*([2] * (n // 2) + ([1] if n % 2 else []) + [1]))
    return (note + "\n\n" if note else "") + t(lang, "email_pick_site"), kb.as_markup()


def email_domains_screen(lang: str, site: dict, rows: list[dict]):
    kb = InlineKeyboardBuilder()
    for i, r in enumerate(rows[:EMAIL_DOMAINS_SHOWN]):
        mark = DELIVERY_MARK.get(r.get("delivery"), "✉️")
        kb.button(text=f"{mark} {r['domain']} · {money(Decimal(r['cents']) / 100)}"[:60], callback_data=Em(a="dom", i=i))
    kb.button(text=t(lang, "btn_back"), callback_data=Em(a="home"))
    kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
    kb.adjust(*([1] * len(rows[:EMAIL_DOMAINS_SHOWN]) + [2]))
    return t(lang, "email_pick_domain", site=esc(site["name"])), kb.as_markup()


def email_confirm_screen(member: Member, lang: str, site: dict, row: dict, i: int, note: str = ""):
    price = Decimal(row["cents"]) / 100
    kb = InlineKeyboardBuilder()
    kb.button(text=t(lang, "btn_email_buy", price=money(price)), callback_data=Em(a="buy", i=i, c=int(row["cents"])))
    kb.button(text=t(lang, "btn_back"), callback_data=Em(a="doms"))
    kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
    kb.adjust(1, 2)
    text = t(lang, "email_confirm", site=esc(site["name"]), domain=esc(row["domain"]), price=money(price),
             balance=money(member.available))
    return (note + "\n\n" if note else "") + text, kb.as_markup()


def _email_rows(rows: list[dict]) -> list[dict]:
    """What the session keeps of a domain list: plain values only."""
    return [{"domain": r["domain"], "cents": int(r["member_price"] * 100), "delivery": r.get("delivery")}
            for r in rows[:EMAIL_DOMAINS_SHOWN]]


def error_text(exc: SellError, reseller: Reseller, member: Member, lang: str) -> str:
    if exc.reason == "no_credit" and payments.connected(reseller):
        return t(lang, "err_no_credit_auto", price=money(exc.price), available=money(member.available))
    if exc.reason == "no_credit":
        return t(lang, "err_no_credit", price=money(exc.price), available=money(member.available),
                 id=member.telegram_id, support=support_label(reseller, lang))
    key = {"paused": "err_paused", "sold_out": "err_sold_out", "too_many_open": "err_too_many",
           "busy": "err_busy", "processing": "err_processing", "blocked": "blocked",
           "email_limit": "err_email_limit"}.get(exc.reason, "err_failed")
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
        data = await state.get_data()
        await state.clear()
        to = callback_data.to
        if to == "apps" and data.get("az"):
            # Back from a country list: to the A–Z page the app was picked on.
            letter, page = data["az"]
            await state.update_data(az=[letter, page])
            await _show(c, *await az_screen(reseller, lang, letter, page))
        elif to in ("buy", "apps"):
            await _show(c, *await services_screen(reseller, lang))
        elif to == "orders":
            await _show(c, *await orders_screen(member, lang))
        elif to in ("balance", "support"):   # support lives on the balance screen now
            await _show(c, *balance_screen(reseller, member, lang))
        elif to == "deposit" and payments.connected(reseller):
            await _show(c, *await pay_amount_screen(reseller, member, lang))
        elif to in ("deposit", "deposit_manual"):
            text, kb, asking = await deposit_start(reseller, member, lang)
            if asking:
                await state.set_state(DepositForm.amount)
            await _show(c, text, kb)
        elif to == "lang":
            kb = InlineKeyboardBuilder()
            for code, label in LANGS.items():
                kb.button(text=("✅ " if code == lang else "") + label, callback_data=Lang(code=code))
            kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
            kb.adjust(2)
            await _show(c, t(lang, "lang_title"), kb.as_markup())
        elif to == "again" and member.last_service and member.last_country:
            try:
                await _show(c, *await confirm_screen(reseller, member, lang, member.last_service,
                                                     member.last_country))
            except SellError:
                await c.answer(t(lang, "err_busy"), show_alert=True)
                return
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
    async def az(c: CallbackQuery, callback_data: AZ, reseller: Reseller, lang: str, state: FSMContext):
        await _show(c, *await az_screen(reseller, lang, callback_data.l, callback_data.page))
        # Remember the page, so Back from a country list comes here again.
        await state.update_data(az=[callback_data.l, callback_data.page] if callback_data.l not in ("", "*")
                                else None)
        await c.answer()

    @r.callback_query(Svc.filter())
    async def svc(c: CallbackQuery, callback_data: Svc, reseller: Reseller, lang: str, state: FSMContext):
        try:
            await _show(c, *await countries_screen(reseller, lang, callback_data.code, callback_data.page))
            # Typing now finds a COUNTRY for this service (falls back to apps),
            # and Back from a country returns to this page of the list.
            await state.update_data(svc=callback_data.code, cpage=callback_data.page)
            await c.answer()
        except SellError:
            await c.answer(t(lang, "err_busy"), show_alert=True)

    @r.callback_query(Cty.filter())
    async def cty(c: CallbackQuery, callback_data: Cty, reseller: Reseller, member: Member, lang: str,
                  state: FSMContext):
        data = await state.get_data()
        page = int(data.get("cpage") or 0) if data.get("svc") == callback_data.code else 0
        try:
            await _show(c, *await confirm_screen(reseller, member, lang, callback_data.code, callback_data.cc,
                                                 back_page=page))
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
            if exc.reason == "duplicate":
                return                  # a double tap: the first tap is buying and shows the card
            fresh = await repo.get_member(member.id) or member
            if exc.reason == "price_changed":
                note = t(lang, "err_price_changed", price=money(exc.price))
                try:
                    await _show(c, *await confirm_screen(reseller, fresh, lang, callback_data.code,
                                                         callback_data.cc, note))
                    return
                except SellError as again:
                    exc = again
            kb = InlineKeyboardBuilder()
            if exc.reason == "no_credit" and (payments.connected(reseller) or reseller.deposit_info):
                kb.button(text=t(lang, "btn_topup"), callback_data=Nav(to="deposit"))
            elif exc.reason == "no_credit" and support_url(reseller):
                kb.button(text=t(lang, "btn_support"), url=support_url(reseller))
            if exc.reason in ("no_credit", "busy", "failed"):
                # Back to the same number, ready to try again.
                kb.button(text=t(lang, "btn_back"), callback_data=Cty(code=callback_data.code, cc=callback_data.cc))
            if exc.reason in ("sold_out",):
                kb.button(text=t(lang, "btn_back"), callback_data=Svc(code=callback_data.code, page=0))
            if exc.reason in ("processing", "too_many_open"):
                kb.button(text=t(lang, "btn_orders"), callback_data=Nav(to="orders"))
            kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
            kb.adjust(1)
            await _show(c, error_text(exc, reseller, fresh, lang), kb.as_markup())
            return
        except Exception:  # noqa: BLE001
            log.exception("buy crashed")
            kb = InlineKeyboardBuilder()
            kb.button(text=t(lang, "btn_orders"), callback_data=Nav(to="orders"))
            kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
            kb.adjust(1)
            await _show(c, t(lang, "err_failed"), kb.as_markup())
            return
        await repo.set_member_last(member.id, callback_data.code, callback_data.cc)
        text, kb = order_card(member, order)
        await _show(c, text, kb)
        await repo.set_card(order.id, c.message.chat.id, c.message.message_id)

    # ── email codes ──
    async def _email_home(c: CallbackQuery, reseller: Reseller, lang: str, state: FSMContext) -> None:
        sites = (await selling.email_sites(reseller))[:EMAIL_SITES_SHOWN] if selling.email_on(reseller) else []
        if not sites:
            await c.answer(t(lang, "err_paused"), show_alert=True)
            return
        await state.set_state(EmailForm.site)
        await state.update_data(em_sites=sites, em_site=None, em_domains=None)
        await _show(c, *email_home_screen(lang, sites))
        await c.answer()

    async def _email_domains(c: CallbackQuery, reseller: Reseller, lang: str, state: FSMContext, site: dict) -> None:
        try:
            rows = _email_rows(await selling.email_domains(reseller, site["site"]))
        except SellError:
            await c.answer(t(lang, "err_busy"), show_alert=True)
            return
        await state.update_data(em_site=site, em_domains=rows)
        if not rows:
            kb = InlineKeyboardBuilder()
            kb.button(text=t(lang, "btn_back"), callback_data=Em(a="home"))
            kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
            kb.adjust(2)
            await _show(c, t(lang, "email_no_domains", site=esc(site["name"])), kb.as_markup())
        else:
            await _show(c, *email_domains_screen(lang, site, rows))
        await c.answer()

    @r.callback_query(Em.filter())
    async def email(c: CallbackQuery, callback_data: Em, reseller: Reseller, member: Member, lang: str,
                    state: FSMContext):
        a, i = callback_data.a, callback_data.i
        data = await state.get_data()
        sites, site, rows = data.get("em_sites") or [], data.get("em_site"), data.get("em_domains") or []
        if a == "site" and 0 <= i < len(sites):
            await _email_domains(c, reseller, lang, state, sites[i])
        elif a == "doms" and site:
            await _email_domains(c, reseller, lang, state, site)
        elif a == "dom" and site and 0 <= i < len(rows):
            await _show(c, *email_confirm_screen(member, lang, site, rows[i], i))
            await c.answer()
        elif a == "buy" and site and 0 <= i < len(rows):
            await _email_buy(c, callback_data, reseller, member, lang, state, site, rows[i])
        else:
            # home, or a button from a list this session no longer has: start again.
            await _email_home(c, reseller, lang, state)

    async def _email_buy(c: CallbackQuery, callback_data: Em, reseller: Reseller, member: Member, lang: str,
                         state: FSMContext, site: dict, row: dict) -> None:
        await c.answer(t(lang, "buying"))
        try:
            order = await selling.buy_email(reseller, member, site["site"], site["name"], row["domain"],
                                            Decimal(callback_data.c) / 100)
        except SellError as exc:
            if exc.reason == "duplicate":
                return                  # a double tap: the first tap is buying and shows the card
            fresh = await repo.get_member(member.id) or member
            if exc.reason == "price_changed":
                # The list with today's prices, and the same domain to confirm again.
                try:
                    rows = _email_rows(await selling.email_domains(reseller, site["site"], fresh=True))
                except SellError:
                    rows = []
                await state.update_data(em_domains=rows)
                at = next((n for n, r in enumerate(rows) if r["domain"] == row["domain"]), None)
                if at is not None:
                    await _show(c, *email_confirm_screen(fresh, lang, site, rows[at], at,
                                                         t(lang, "err_price_changed", price=money(exc.price))))
                    return
                exc = SellError("sold_out")
            kb = InlineKeyboardBuilder()
            if exc.reason == "no_credit" and (payments.connected(reseller) or reseller.deposit_info):
                kb.button(text=t(lang, "btn_topup"), callback_data=Nav(to="deposit"))
            elif exc.reason == "no_credit" and support_url(reseller):
                kb.button(text=t(lang, "btn_support"), url=support_url(reseller))
            if exc.reason in ("processing", "too_many_open"):
                kb.button(text=t(lang, "btn_orders"), callback_data=Nav(to="orders"))
            else:
                kb.button(text=t(lang, "btn_back"), callback_data=Em(a="doms"))
            kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
            kb.adjust(1)
            await _show(c, error_text(exc, reseller, fresh, lang), kb.as_markup())
            return
        except Exception:  # noqa: BLE001
            log.exception("email buy crashed")
            kb = InlineKeyboardBuilder()
            kb.button(text=t(lang, "btn_orders"), callback_data=Nav(to="orders"))
            kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
            kb.adjust(1)
            await _show(c, t(lang, "err_failed"), kb.as_markup())
            return
        await state.clear()
        text, kb = order_card(member, order)
        await _show(c, text, kb)
        await repo.set_card(order.id, c.message.chat.id, c.message.message_id)

    @r.message(StateFilter(EmailForm.site), F.text, ~F.text.startswith("/"))
    async def email_typed(m: Message, reseller: Reseller, lang: str, state: FSMContext):
        found = await selling.email_search(reseller, m.text) if selling.email_on(reseller) else []
        if not found:
            sites = (await state.get_data()).get("em_sites") or []
            await _show(m, *email_home_screen(lang, sites, t(lang, "email_none_found")))
            return
        await state.update_data(em_sites=found, em_site=None, em_domains=None)
        await _show(m, *email_home_screen(lang, found))

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

    # ── deposits: the customer asks for balance, the owner approves ──
    @r.message(StateFilter(DepositForm.amount), F.text, ~F.text.startswith("/"))
    async def dep_amount(m: Message, reseller: Reseller, member: Member, lang: str, state: FSMContext):
        amount = parse_amount(m.text)
        if amount is None or amount > DEPOSIT_MAX:
            await m.answer(t(lang, "dep_bad_amount"), reply_markup=_cancel_member(lang))
            return
        if reseller.deposit_min and amount < reseller.deposit_min:
            await m.answer(t(lang, "dep_below_min", min=money(reseller.deposit_min)), reply_markup=_cancel_member(lang))
            return
        await state.update_data(dep_amount=str(amount))
        await state.set_state(DepositForm.proof)
        await m.answer(t(lang, "dep_proof"), reply_markup=_cancel_member(lang))

    @r.message(StateFilter(DepositForm.proof))
    async def dep_proof(m: Message, reseller: Reseller, member: Member, lang: str, state: FSMContext):
        photo = m.photo[-1].file_id if m.photo else None
        text = (m.text or m.caption or "").strip()
        if text.startswith("/") and not photo:
            await state.clear()
            await _show(m, *await menu_screen(reseller, member, lang, member.telegram_id == reseller.owner_id))
            return
        if not photo and not text:
            await m.answer(t(lang, "dep_bad_proof"), reply_markup=_cancel_member(lang))
            return
        data = await state.get_data()
        await state.clear()
        try:
            amount = Decimal(str(data.get("dep_amount")))
        except (InvalidOperation, ValueError):
            await _show(m, *balance_screen(reseller, member, lang))
            return
        dep = await repo.create_deposit(reseller.id, member.id, amount, text[:300] or None, photo)
        if dep is None:
            waiting = await repo.pending_deposit(member.id)
            await m.answer(t(lang, "dep_pending", n=waiting.number if waiting else "?",
                             amount=money(waiting.amount if waiting else amount)))
            return
        from app.bots import admin
        await admin.notify_owner_deposit(m.bot, reseller, member, dep)
        await m.answer(t(lang, "dep_sent", n=dep.number, amount=money(dep.amount)))

    # ── automatic top-ups: the owner's own OxaPay / @CryptoBot (app/payments.py) ──
    def _menu_kb(lang: str):
        kb = InlineKeyboardBuilder()
        kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
        return kb.as_markup()

    async def _after_amount(target, reseller: Reseller, member: Member, lang: str, amount: Decimal):
        gates = payments.connected(reseller)
        if not gates:
            await _show(target, *balance_screen(reseller, member, lang))
            return
        if len(gates) == 1 and gates[0] != payments.NUMBERHUB:
            await _make_link(target, reseller, member, lang, gates[0], amount)
            return
        # Several ways to pay, or built-in USDT (the customer picks the network).
        kb = InlineKeyboardBuilder()
        cents = int(amount * 100)
        if payments.OXAPAY in gates:
            kb.button(text=t(lang, "btn_pay_oxapay"), callback_data=Pay(a="via", g="ox", c=cents))
        if payments.CRYPTOBOT in gates:
            kb.button(text=t(lang, "btn_pay_cryptobot"), callback_data=Pay(a="via", g="cb", c=cents))
        qris = payments.KASERA in gates and payments.kasera_fits(reseller, amount)
        if qris:
            kb.button(text=t(lang, "btn_pay_qris"), callback_data=Pay(a="via", g="kq", c=cents))
        sizes = [1] * (sum(1 for g in (payments.OXAPAY, payments.CRYPTOBOT) if g in gates) + (1 if qris else 0))
        if payments.NUMBERHUB in gates:
            # Every coin the shop can take, three to a row; the network comes next.
            names = payments.coins(await payments.methods(reseller), amount)     # only coins that take this amount
            for coin in names:
                kb.button(text=f"{COIN_ICON.get(coin, '🪙')} {coin}", callback_data=Pay(a="coin", g=coin, c=cents))
            sizes += [3] * (len(names) // 3) + ([len(names) % 3] if len(names) % 3 else [])
        kb.button(text=t(lang, "btn_cancel"), callback_data=Nav(to="menu"))
        kb.adjust(*sizes, 1)
        await _show(target, t(lang, "pay_choose_method", amount=money(amount)), kb.as_markup())

    async def _pick_network(target, reseller: Reseller, member: Member, lang: str, coin: str, amount: Decimal):
        """A coin was chosen: straight to the address when it has one network, else the networks."""
        table = payments.for_amount(await payments.methods(reseller), amount)
        nets = [(mid, m) for mid, m in table.items() if m["coin"] == coin]
        if payments.NUMBERHUB not in payments.connected(reseller) or not nets:
            await _show(target, *await pay_amount_screen(reseller, member, lang))
            return
        if len(nets) == 1:
            await _make_link(target, reseller, member, lang, payments.NUMBERHUB, amount, nets[0][0])
            return
        kb = InlineKeyboardBuilder()
        for mid, m in nets:
            kb.button(text=f"{COIN_ICON.get(coin, '🪙')} {coin} · {m['network']}",
                      callback_data=Pay(a="via", g=mid, c=int(amount * 100)))
        kb.button(text=t(lang, "btn_cancel"), callback_data=Nav(to="menu"))
        kb.adjust(1)
        await _show(target, t(lang, "pay_choose_method", amount=money(amount)), kb.as_markup())

    async def _make_link(target, reseller: Reseller, member: Member, lang: str, provider: str, amount: Decimal,
                         method: str | None = None):
        # One at a time per customer: taps sent together must not each see room under the limits.
        async with payments.member_lock(member.id):
            await _make_link_locked(target, reseller, member, lang, provider, amount, method)

    async def _make_link_locked(target, reseller: Reseller, member: Member, lang: str, provider: str,
                                amount: Decimal, method: str | None = None):
        low, high = pay_limits(reseller)
        if not low <= amount <= high or provider not in payments.connected(reseller):
            await _show(target, *await pay_amount_screen(reseller, member, lang))
            return
        if (len(await repo.open_payments(member.id)) >= payments.OPEN_PER_MEMBER
                or await repo.payments_today(member.id) >= payments.PER_DAY_PER_MEMBER):
            await _show(target, t(lang, "pay_too_many"), _menu_kb(lang))
            return
        try:
            p = await payments.open_link(reseller, member.id, provider, amount, method)
        except payments.PayError as exc:
            log.warning("reseller %s: %s payment link failed: %s", reseller.id, provider, exc)
            bot = target.bot if isinstance(target, Message) else target.message.bot
            await payments.warn_owner_once(bot, reseller, provider, str(exc))
            await _show(target, t(lang, "pay_unavailable", support=support_label(reseller, lang)), _menu_kb(lang))
            return
        if provider == payments.NUMBERHUB:
            await _show(target, *address_screen(p, lang))
            return
        if provider == payments.KASERA:
            await send_qris(target, p, lang)
            return
        kb = InlineKeyboardBuilder()
        kb.button(text=t(lang, "btn_pay_now", amount=money(p.amount)), url=p.pay_url)
        kb.button(text=t(lang, "btn_pay_check"), callback_data=Pay(a="chk", id=p.id))
        kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
        kb.adjust(1)
        await _show(target, t(lang, "pay_invoice", amount=money(p.amount), minutes=payments.INVOICE_MINUTES),
                    kb.as_markup())

    @r.callback_query(Pay.filter())
    async def pay_cb(c: CallbackQuery, callback_data: Pay, reseller: Reseller, member: Member, lang: str,
                     state: FSMContext):
        a = callback_data.a
        await state.clear()
        if a == "other":
            low, high = pay_limits(reseller)
            await state.set_state(PayForm.amount)
            await _show(c, t(lang, "pay_type_amount") + "\n" + t(lang, "dep_min", min=money(low)),
                        _cancel_member(lang))
        elif a == "amt":
            await _after_amount(c, reseller, member, lang, Decimal(callback_data.c) / 100)
        elif a == "via" and callback_data.g in GATEWAY:
            await _make_link(c, reseller, member, lang, GATEWAY[callback_data.g], Decimal(callback_data.c) / 100)
        elif a == "coin":
            await _pick_network(c, reseller, member, lang, callback_data.g.upper(), Decimal(callback_data.c) / 100)
        elif a == "via":
            # A network of a built-in coin (or one of the two codes old buttons carry).
            await _make_link(c, reseller, member, lang, payments.NUMBERHUB, Decimal(callback_data.c) / 100,
                             NETWORK.get(callback_data.g, callback_data.g))
        elif a == "show":
            p = await repo.get_payment(callback_data.id)
            if p is None or p.member_id != member.id:
                await c.answer(t(lang, "toast_stale"), show_alert=True)
                return
            still = p.expires_at is None or (p.expires_at if p.expires_at.tzinfo
                                             else p.expires_at.replace(tzinfo=dt.timezone.utc)) > dt.datetime.now(dt.timezone.utc)
            shown = payments.details(p)
            if p.status != p.PENDING or not still or not (shown.get("address") or shown.get("qr")):
                await c.answer(t(lang, "pay_already" if p.status == p.PAID else "pay_expired"), show_alert=True)
                return
            if shown.get("qr"):
                await send_qris(c, p, lang)
            else:
                await _show(c, *address_screen(p, lang))
        elif a == "chk":
            p = await repo.get_payment(callback_data.id)
            if p is None or p.member_id != member.id:
                await c.answer(t(lang, "toast_stale"), show_alert=True)
                return
            if p.status == p.PENDING:
                state_ = await payments.check(p, reseller, c.bot)
            elif p.status == p.EXPIRED and p.provider == payments.NUMBERHUB:
                state_ = await payments.check_late(p, reseller, c.bot)     # paid after it was given up?
            else:
                state_ = "already"
            if state_ == "paid":
                fresh = await repo.get_member(member.id) or member
                p = await repo.get_payment(p.id)
                await _show(c, t(lang, "pay_received", amount=money(p.credited), balance=money(fresh.available)),
                            _menu_kb(lang))
            else:
                key = {"already": "pay_already", "expired": "pay_expired", "manual": "pay_manual",
                       "failed": "pay_expired"}.get(state_, "pay_not_yet")
                if state_ == "already" and p.status != p.PAID:
                    key = "pay_expired" if p.status in (p.EXPIRED, p.FAILED) else "pay_manual"
                await c.answer(t(lang, key), show_alert=True)
                return
        else:
            await _show(c, *await pay_amount_screen(reseller, member, lang))
        await c.answer()

    @r.message(StateFilter(PayForm.amount), F.text, ~F.text.startswith("/"))
    async def pay_amount_typed(m: Message, reseller: Reseller, member: Member, lang: str, state: FSMContext):
        low, high = pay_limits(reseller)
        amount = parse_amount(m.text)
        if amount is None or not low <= amount <= high:
            await m.answer(t(lang, "pay_bad_amount", min=money(low), max=money(high)),
                           reply_markup=_cancel_member(lang))
            return
        await state.clear()
        await _after_amount(m, reseller, member, lang, amount)

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
            # By name, or by the 2-letter code (de, us, gb) that works in any language.
            hits = [row for row in rows if ql in (row.get("name") or "").lower()
                    or (len(ql) == 2 and ql == (row.get("flag") or "").lower())]
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
        tail = []
        if svc_code:
            # Typed on a country list and matched nothing there: the way back to it.
            kb.button(text=t(lang, "btn_back"), callback_data=Svc(code=svc_code, page=0))
            tail.append(1)
        kb.button(text=t(lang, "btn_all_services", n=len(items)), callback_data=AZ())
        kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
        kb.adjust(*_pairs(len(found)), *tail, 1, 1)
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
    if not v.is_finite() or v > 100000:
        return None
    v = v.quantize(Decimal("0.01"))
    return v if v > 0 else None        # "0.004" would have added "$0.00"
