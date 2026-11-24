"""Rendering: order cards and member messages (translated), owner alerts."""
from __future__ import annotations

import datetime as dt
import html
import re
from decimal import Decimal

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bots.callbacks import Em, Nav, Ord, Svc
from app.i18n import t
from app.models import Member, Order
from app.numberhub import flag


def money(x) -> str:
    return f"${Decimal(str(x or 0)):.2f}"


def esc(x) -> str:
    return html.escape(str(x or ""), quote=False)


def phone_fmt(phone: str | None) -> str:
    p = str(phone or "").strip()
    return ("+" + p.lstrip("+")) if p else "—"


def _aware(v: dt.datetime | None) -> dt.datetime | None:
    if v is None:
        return None
    return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def seconds_until(v: dt.datetime | None) -> int:
    v = _aware(v)
    return max(0, int((v - _now()).total_seconds())) if v else 0


def mmss(secs: int) -> str:
    return f"{secs // 60:02d}:{secs % 60:02d}"


# Status lines that speak of an SMS or a number have a wording of their own for a mailbox.
_EMAIL_WORDING = {"st_waiting", "st_received", "st_expired", "st_buying", "st_failed", "card_held"}


def order_card(member: Member, order: Order) -> tuple[str, InlineKeyboardMarkup]:
    from app.selling import order_codes
    lang = member.language or "en"
    codes = order_codes(order)
    email = getattr(order, "kind", "sms") == "email"

    def k(key: str) -> str:
        return "email_" + key if email and key in _EMAIL_WORDING else key
    emoji = flag(order.country_iso)
    if email:
        # A mailbox for one mail from one site: the address instead of a number.
        lines = [t(lang, "email_card_title", site=esc(order.service_name or order.service))]
        if order.phone:
            lines.append(t(lang, "email_card_address", address=esc(order.phone)))
    else:
        lines = [t(lang, "card_title", service=esc(order.service_name or order.service), flag=emoji,
                   country=esc(order.country_name or order.country))]
        if order.phone:
            lines.append(t(lang, "card_number", phone=esc(phone_fmt(order.phone))))
    lines.append("")
    st = order.status
    if st not in ("buying", "pending", "waiting", "completed", "canceled", "expired", "failed"):
        st = "received"           # received, or a transient NumberHub status after a code
    if st == "waiting":
        lines.append(t(lang, k("st_waiting"), left=mmss(seconds_until(order.expires_at))))
        if not codes:
            lines.append(t(lang, "email_card_howto" if email else "card_howto"))
    elif st == "canceled" and not codes:
        lines.append(t(lang, "st_canceled", price=money(order.member_price)))
    elif st == "expired" and not codes and not (email and order.settled == Order.CHARGED):
        lines.append(t(lang, k("st_expired"), price=money(order.member_price)))
    elif st in ("received", "completed", "canceled", "expired"):
        lines.append(t(lang, k("st_received") if st == "received" else "st_completed"))
    else:
        lines.append(t(lang, k(f"st_{st}"), price=money(order.member_price)))
    if codes:
        lines.append(t(lang, "card_code", code=esc(codes[-1])))
        hint = "" if email else flash_hint(lang, codes[-1])
        if hint:
            lines.append(hint)
        if len(codes) > 1:
            lines.append(t(lang, "card_more_codes", codes=", ".join(f"<code>{esc(c)}</code>" for c in codes[:-1])))
    if st in ("buying", "pending", "waiting"):
        lines.append(t(lang, k("card_held"), price=money(order.member_price)))
    elif codes or st in ("received", "completed"):
        lines.append(t(lang, "card_paid", price=money(order.member_price)))
    if (order.phone or codes) and not email:
        lines += ["", t(lang, "card_tip")]

    kb = InlineKeyboardBuilder()
    if st in ("pending", "waiting"):
        lock = seconds_until(order.cancel_available_at) if st == "waiting" else 0
        if lock > 0:
            kb.button(text=t(lang, "btn_cancel_in", s=lock), callback_data=Ord(a="cancel", id=order.id))
        else:
            kb.button(text=t(lang, "btn_cancel"), callback_data=Ord(a="cancel", id=order.id))
        kb.button(text=t(lang, "btn_refresh"), callback_data=Ord(a="view", id=order.id))
    elif email and st not in ("buying",):
        kb.button(text=t(lang, "btn_email_again"), callback_data=Em(a="home"))
    elif st in ("canceled", "expired", "failed") and not codes:
        kb.button(text=t(lang, "btn_new_number"), callback_data=Svc(code=order.service, page=0))
    elif st in ("received", "completed"):
        kb.button(text=t(lang, "btn_new_number"), callback_data=Svc(code=order.service, page=0))
    kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
    kb.adjust(2)
    return "\n".join(lines), kb.as_markup()


_CALLER = re.compile(r"^\+?\d{10,15}$")


def flash_hint(lang: str, code: str) -> str:
    """Some apps verify with a call: NumberHub then hands over the CALLER's
    number (441616961154) and the code is its last digits. Say so, or the
    member types 12 digits, the app refuses, and they paid for nothing."""
    digits = str(code or "").strip()
    if not _CALLER.match(digits):
        return ""
    digits = digits.lstrip("+")
    return t(lang, "code_flash", last6=digits[-6:], last4=digits[-4:])


def code_arrived(member: Member, order: Order, code: str) -> str:
    lang = member.language or "en"
    text = t(lang, "msg_code", code=esc(code), service=esc(order.service_name or order.service),
             phone=esc(phone_fmt(order.phone)))
    hint = flash_hint(lang, code)
    return f"{text}\n\n{hint}" if hint else text


def mail_arrived(member: Member, order: Order, mail: dict, codes: list[str]) -> str:
    """The mail an email order was waiting for: its code, and its link when it has
    one (many sites confirm with a link and no code), else the start of its text."""
    lang = member.language or "en"
    site = esc(order.service_name or order.service)
    code = str(mail.get("code") or (codes[-1] if codes else "") or "").strip()
    link = str(mail.get("link") or "").strip()
    lines = [t(lang, "email_msg_code", site=site, code=esc(code)) if code
             else t(lang, "email_msg_mail", site=site, address=esc(order.phone or ""))]
    if link.startswith(("https://", "http://")):
        lines.append(t(lang, "email_msg_link", link=esc(link[:900])))
    elif not code:
        body = " ".join(str(mail.get("message") or "").split())
        if body:
            lines += ["", esc(body[:700])]
    return "\n".join(lines)


def number_ready(member: Member, order: Order) -> str:
    return t(member.language or "en", "msg_ready", phone=esc(phone_fmt(order.phone)),
             service=esc(order.service_name or order.service))


def refunded(member: Member, order: Order) -> str:
    if getattr(order, "kind", "sms") == "email":
        return t(member.language or "en", "email_msg_refunded", site=esc(order.service_name or order.service),
                 price=money(order.member_price))
    return t(member.language or "en", "msg_refunded", service=esc(order.service_name or order.service),
             price=money(order.member_price))


_NEW_KEY = ("Send a new key in the bot where you created this shop: 🤖 My bots → 🔑 New API key. "
            "Use a key from the same NumberHub account, so the open orders keep working.")
OWNER_ALERTS = {
    "low_balance": ("⚠️ <b>A customer could not buy: your NumberHub balance is too low.</b>\n"
                    "Top up at numberhub.io or in @TheNumberHubBot to keep selling."),
    "daily_limit": ("⚠️ <b>Sales stopped: your NumberHub API key reached its daily spend limit.</b>\n"
                    "It counts what your numbers really cost; numbers that got no code don't count. "
                    "Raise or remove it at numberhub.io → Account → API keys."),
    "bad_key": "⚠️ <b>NumberHub rejected your API key</b> (revoked or rotated), so sales are paused.\n" + _NEW_KEY,
    "key_scope": ("⚠️ <b>Your NumberHub API key is missing a permission</b>, so sales are paused.\n"
                  "The key needs catalog, orders (read and write) and wallet access. " + _NEW_KEY),
    "key_ip": ("⚠️ <b>Your NumberHub API key only works from certain IP addresses</b>, and this "
               "server is not one of them, so sales are paused.\nRemove the IP limit on the key at "
               "numberhub.io → Account → API keys, or " + _NEW_KEY[0].lower() + _NEW_KEY[1:]),
    "by_hand_unbacked": ("⚠️ <b>A customer could not buy: their balance was added by hand, and your "
                         "NumberHub wallet has none of your own money behind it.</b>\n"
                         "What is in your wallet now was paid in by customers through built-in payments "
                         "and is kept for them. Top up your wallet at numberhub.io or in @TheNumberHubBot: "
                         "then a balance you add by hand, approve as a deposit or take through your own "
                         "gateway can buy."),
    "route_cap": ("⚠️ <b>A customer could not buy: your NumberHub account has the most open orders it "
                  "may have for one app and country</b> (all your shops count together). It frees up "
                  "as those orders finish."),
}


def owner_alert(reason: str) -> str:
    return OWNER_ALERTS.get(reason, "⚠️ Sales are paused.")
