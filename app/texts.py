"""Rendering: order cards and member messages (translated), owner alerts."""
from __future__ import annotations

import datetime as dt
import html
import re
from decimal import Decimal

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bots.callbacks import Cty, Em, Nav, Ord, Svc
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
    if getattr(order, "kind", "sms") == "rent":
        return rental_card(member, order)
    if getattr(order, "kind", "sms") == "esim":
        # Without NumberHub's live answer: the short card; opening it loads the rest.
        return esim_card(member, order, None)
    if getattr(order, "kind", "sms") == "proxy":
        # Whoever draws a card without NumberHub's live answer (a recovered purchase,
        # a refresh) gets the short one; opening it loads the rest.
        return proxy_card(member, order, None)
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


def after_credit_kb(member: Member) -> InlineKeyboardMarkup:
    """Under "money added": straight back to the number the customer was about to buy
    when their balance fell short (kept for a day), else to the shop."""
    from app import selling
    lang = member.language or "en"
    kb = InlineKeyboardBuilder()
    route = selling.remembered_route(member.id)
    if route:
        code, cc, name, emoji = route
        kb.button(text=t(lang, "btn_continue", service=name[:24], flag=emoji).strip(), callback_data=Cty(code=code, cc=cc))
    else:
        kb.button(text=t(lang, "btn_buy"), callback_data=Nav(to="buy"))
    kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
    kb.adjust(1)
    return kb.as_markup()


def proxy_card(member: Member, order: Order, live: dict | None = None):
    """A data package. With NumberHub's live answer (`live`: its order with the
    `proxy` block) the card says how to connect and what is left; without it (not
    asked yet, or it could not be read) it says what was bought and offers to load
    the rest. The host shown is the gateway's address in numbers: a shop is its
    owner's brand, and some apps refuse a name anyway."""
    from urllib.parse import quote

    from app.bots.callbacks import Px
    lang = member.language or "en"
    lines = [t(lang, "proxy_card_title", gb=esc(order.country))]
    p = live.get("proxy") if isinstance(live, dict) and isinstance(live.get("proxy"), dict) else None
    until = order.expires_at
    if until is not None and until.tzinfo is None:
        until = until.replace(tzinfo=dt.timezone.utc)
    now = dt.datetime.now(dt.timezone.utc)
    st = order.status
    ended = st in ("completed", "canceled", "expired") or (until is not None and until <= now)
    active = st == "received" and not ended
    if st == "failed":
        lines.append(t(lang, "proxy_failed", price=money(order.member_price)))
    elif st == "buying":
        lines.append(t(lang, "err_processing"))
    elif ended:
        lines.append(t(lang, "proxy_ended"))
    elif until is not None:
        lines.append(t(lang, "proxy_active", date=until.strftime("%Y-%m-%d"),
                       days=max(0, int((until - now).total_seconds() // 86400))))
    usage = p.get("usage") if p else None
    if isinstance(usage, dict) and usage.get("total"):
        lines.append(t(lang, "proxy_usage", used=esc(usage.get("used")), total=esc(usage.get("total")),
                       left=esc(usage.get("left"))))
    host = str((p or {}).get("ip") or (p or {}).get("host") or "")
    user, password = str((p or {}).get("username") or ""), str((p or {}).get("password") or "")
    shown = bool(active and host and user and password)
    if shown:
        http, socks = p.get("http_port") or "", p.get("socks_port") or ""
        ready = p.get("lines") if isinstance(p.get("lines"), dict) else {}
        lines += ["", t(lang, "proxy_access", host=esc(host), http=esc(http), socks=esc(socks), login=esc(user),
                        password=esc(password),
                        any=esc(ready.get("any") or f"{host}:{http}:{user}:{password}"),
                        country=esc(ready.get("country") or f"{host}:{http}:{user}-country-us:{password}"),
                        sticky=esc(ready.get("sticky") or f"{host}:{http}:{user}-country-us-sid-a1b2c3:{password}"))]
    elif active:
        lines += ["", t(lang, "proxy_card_closed")]
    if order.settled == Order.CHARGED:
        lines += ["", t(lang, "card_paid", price=money(order.member_price))]
    kb = InlineKeyboardBuilder()
    sizes = []
    if active:
        kb.button(text=t(lang, "btn_refresh" if shown else "btn_proxy_show"), callback_data=Ord(a="view", id=order.id))
        sizes.append(1)
        if shown and p.get("socks_port"):
            # Telegram's own proxy link: one tap sets it up in the app.
            kb.button(text=t(lang, "btn_proxy_tg"),
                      url=f"https://t.me/socks?server={quote(host)}&port={quote(str(p['socks_port']))}"
                          f"&user={quote(user)}&pass={quote(password)}")
            sizes.append(1)
    if st != "buying":
        kb.button(text=t(lang, "btn_proxy"), callback_data=Px(a="home"))
    kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
    kb.adjust(*sizes, 2)
    return "\n".join(lines), kb.as_markup()


def esim_card(member: Member, order: Order, live: dict | None = None):
    """An eSIM. With NumberHub's live answer (`live`: its order with the `esim` block)
    a delivered one says how to install it: one tap, the QR, or the two codes typed by
    hand. Without it the card says what was bought and offers to load the rest."""
    from app.bots.callbacks import Es
    from app.selling import esim_profile
    lang = member.language or "en"
    st = order.status
    p = esim_profile(live)
    lines = [f"🌍 <b>eSIM · {esc(order.service_name or order.service)}</b>",
             f"{flag(order.country_iso) if order.country_iso else '🌍'} {esc(order.country_name or order.country)}", ""]
    ready = st in Order.CHARGED_STATUSES
    if st == "buying":
        lines.append(t(lang, "err_processing"))
    elif st == "failed":
        lines.append(t(lang, "esim_failed", price=money(order.member_price)))
    elif st in ("canceled", "expired"):
        lines.append(t(lang, "esim_refunded", price=money(order.member_price)))
    elif not ready:
        lines.append(t(lang, "esim_preparing"))
    else:
        lines.append(t(lang, "esim_ready"))
        if p:
            lines += ["", t(lang, "esim_install", smdp=esc(p.get("smdp") or ""), code=esc(p.get("matchingId") or ""),
                            ac=esc(p.get("ac") or ""), iccid=esc(p.get("iccid") or order.phone or ""))]
        else:
            lines += ["", t(lang, "esim_card_closed")]
    if order.settled == Order.CHARGED:
        lines += ["", t(lang, "card_paid", price=money(order.member_price))]
    elif st in ("buying", "pending", "waiting"):
        lines += ["", t(lang, "esim_held", price=money(order.member_price))]
    kb = InlineKeyboardBuilder()
    sizes = []
    if ready and p:
        links = p.get("install") if isinstance(p.get("install"), dict) else {}
        urls = [(k, str(links.get(k) or "")) for k in ("ios", "android")]
        urls = [(k, u) for k, u in urls if u.startswith("https://esimsetup.")]
        for k, u in urls:
            kb.button(text=t(lang, "btn_esim_ios" if k == "ios" else "btn_esim_android"), url=u)
        if urls:
            sizes.append(len(urls))
        kb.button(text=t(lang, "btn_esim_qr"), callback_data=Ord(a="qr", id=order.id))
        sizes.append(1)
    elif ready or st in ("pending", "waiting"):
        kb.button(text=t(lang, "btn_esim_show" if ready else "btn_refresh"), callback_data=Ord(a="view", id=order.id))
        sizes.append(1)
    if st != "buying":
        kb.button(text=t(lang, "btn_esim"), callback_data=Es(a="home"))
    kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
    kb.adjust(*sizes, 2)
    return "\n".join(lines), kb.as_markup()


RENT_SMS_SHOWN = 5              # the newest SMS a rented number's card lists


def rent_period(lang: str, hours) -> str:
    """'1 day', '1 week': a rental period in the customer's language."""
    try:
        h = int(hours or 0)
    except (TypeError, ValueError):
        h = 0
    if h in (24, 72, 168, 720):
        return t(lang, f"rent_p_{h}")
    return t(lang, "rent_p_days", n=max(1, -(-h // 24)))


def days_label(lang: str, n) -> str:
    """'1 day', '7 days': a number of days in the customer's language."""
    n = int(n or 0)
    return t(lang, "rent_p_24") if n == 1 else t(lang, "rent_p_72") if n == 3 else t(lang, "rent_p_days", n=n)


def rent_service_label(lang: str, code: str, name: str | None) -> str:
    """What a number is rented for. "Any service" (every SMS) and "any other" are
    said in the customer's language; an app keeps its name."""
    if code == "full":
        return t(lang, "rent_any_service")
    if code == "ot":
        return t(lang, "btn_any_other")
    return str(name or code)


def _sms_lines(lang: str, m: dict, limit: int) -> list[str]:
    sender = str(m.get("from") or "").strip()
    text = " ".join(str(m.get("text") or "").split())
    if len(text) > limit:
        text = text[:limit - 1] + "…"
    code = str(m.get("code") or "").strip()
    out = [f"📩 <b>{esc(sender)}</b>: {esc(text)}" if sender else f"📩 {esc(text or code)}"]
    if code:
        out.append(t(lang, "card_code", code=esc(code)))
    return out


def rental_card(member: Member, order: Order) -> tuple[str, InlineKeyboardMarkup]:
    """A rented number: until when it is the customer's, its newest SMS, and, while
    NumberHub still gives the money back for it, how long that lasts."""
    from app.bots.callbacks import Rn
    from app.selling import order_messages
    lang = member.language or "en"
    now, until, st = _now(), _aware(order.expires_at), order.status
    sms = order_messages(order)
    lines = [t(lang, "rent_card_title", service=esc(rent_service_label(lang, order.service, order.service_name)),
               flag=flag(order.country_iso), country=esc(order.country_name or order.country))]
    if order.phone:
        lines.append(t(lang, "card_number", phone=esc(phone_fmt(order.phone))))
    lines.append("")
    active = False
    if st == "buying":
        lines.append(t(lang, "err_processing"))
    elif st == "failed":
        lines.append(t(lang, "rent_failed", price=money(order.member_price)))
    elif st == "canceled":
        lines.append(t(lang, "rent_canceled", price=money(order.member_price)))
    elif st in ("completed", "expired") or (until is not None and until <= now):
        lines.append(t(lang, "rent_ended"))
    else:
        active = True
        if until is not None:
            left, date = int((until - now).total_seconds()), until.strftime("%Y-%m-%d %H:%M")
            lines.append(t(lang, "rent_active_days", date=date, days=left // 86400) if left >= 2 * 86400
                         else t(lang, "rent_active_hours", date=date, hours=max(1, -(-left // 3600))))
    if sms:
        lines.append("")
        if len(sms) > RENT_SMS_SHOWN:
            lines.append(t(lang, "rent_more_sms", n=len(sms) - RENT_SMS_SHOWN))
        for one in sms[-RENT_SMS_SHOWN:]:
            lines += _sms_lines(lang, one, 280)
    elif active:
        lines += ["", t(lang, "rent_no_sms")]
    lock = end = 0
    if active and not sms and order.settled == Order.UNSETTLED and getattr(order, "refund_until", None) is not None:
        end, lock = seconds_until(order.refund_until), seconds_until(order.cancel_available_at)
    if end > 0:
        lines += ["", t(lang, "rent_refund_soon", price=money(order.member_price), left=mmss(lock)) if lock > 0
                  else t(lang, "rent_refund_open", price=money(order.member_price), left=mmss(end))]
    if st not in ("buying", "failed", "canceled"):
        lines += ["", t(lang, "card_paid", price=money(order.member_price))]
    if order.phone and st not in ("failed", "canceled"):
        lines.append(t(lang, "card_tip"))
    kb = InlineKeyboardBuilder()
    sizes = []
    if end > 0:
        kb.button(text=t(lang, "btn_cancel_in", s=lock) if lock > 0 else t(lang, "btn_rent_cancel"),
                  callback_data=Ord(a="cancel", id=order.id))
        sizes.append(1)
    if active:
        kb.button(text=t(lang, "btn_refresh"), callback_data=Ord(a="view", id=order.id))
    if st != "buying":
        kb.button(text=t(lang, "btn_rent"), callback_data=Rn(a="home"))
    kb.button(text=t(lang, "btn_menu"), callback_data=Nav(to="menu"))
    kb.adjust(*sizes, 2)
    return "\n".join(lines), kb.as_markup()


def rental_sms(member: Member, order: Order, m: dict) -> str:
    """An SMS that arrived on a rented number, in full."""
    lang = member.language or "en"
    return "\n".join([t(lang, "rent_msg_sms", phone=esc(phone_fmt(order.phone)))] + _sms_lines(lang, m, 900))


def rental_refunded(member: Member, order: Order) -> str:
    return t(member.language or "en", "rent_msg_refunded", phone=esc(phone_fmt(order.phone)),
             price=money(order.member_price))


def rental_ended(member: Member, order: Order) -> str:
    return t(member.language or "en", "rent_msg_ended", phone=esc(phone_fmt(order.phone)))


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
    if getattr(order, "kind", "sms") == "esim":
        return t(member.language or "en", "esim_msg_refunded", plan=esc(order.service_name or order.service),
                 price=money(order.member_price))
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
    "proxy_unpaid": ("⚠️ <b>A proxy data package was bought on your NumberHub account, and your customer's "
                     "balance no longer covered it.</b>\nIt could not be given back. You find it in your "
                     "NumberHub account under your orders: use it yourself, or pass its details to the "
                     "customer once they have paid you."),
    "proxy_unconfirmed": ("⚠️ <b>A proxy data purchase got no clear answer from NumberHub for 20 hours</b>, so "
                          "your customer got their money back.\nIf a package from that time shows in your "
                          "NumberHub account under your orders, your wallet paid for it: pass its details to "
                          "the customer once they have paid you, or use it yourself."),
    "esim_unpaid": ("⚠️ <b>An eSIM was bought on your NumberHub account, and your customer's balance no "
                    "longer covered it.</b>\nIt cannot be given back. You find it in your NumberHub account under "
                    "your orders, with its QR code: use it yourself, or pass it to the customer once they have "
                    "paid you."),
    "esim_unconfirmed": ("⚠️ <b>An eSIM purchase got no clear answer from NumberHub for 20 hours</b>, so your "
                         "customer got their money back.\nIf an eSIM from that time shows in your NumberHub "
                         "account under your orders, your wallet paid for it: pass its QR code to the customer "
                         "once they have paid you, or use it yourself."),
    "rent_unpaid": ("⚠️ <b>A number was rented on your NumberHub account, and your customer's balance no "
                    "longer covered it.</b>\nIt could not be given back. You find it in your NumberHub account "
                    "under your orders, with every SMS it receives: use it yourself, or pass it to the customer "
                    "once they have paid you."),
    "rent_unconfirmed": ("⚠️ <b>A number rental got no clear answer from NumberHub for 20 hours</b>, so your "
                         "customer got their money back.\nIf a rental from that time shows in your NumberHub "
                         "account under your orders, your wallet paid for it: pass the number to the customer "
                         "once they have paid you, or use it yourself."),
    "number_cycler": ("⚠️ <b>New numbers are paused for one customer until tomorrow.</b>\n"
                      "Customer {who}<code>{id}</code> took {n} numbers in a day and {got} got a code. Your "
                      "NumberHub account is judged on how many of your numbers get a code, and it pauses new "
                      "numbers for ALL your customers when that falls too low. This keeps one customer from "
                      "using that up. Nothing was charged to them, and it lifts by itself as the day passes: "
                      "you do not need to do anything. Email codes and proxy data still work for them."),
    "route_cap": ("⚠️ <b>A customer could not buy: your NumberHub account has the most open orders it "
                  "may have for one app and country</b> (all your shops count together). It frees up "
                  "as those orders finish."),
}


def owner_alert(reason: str) -> str:
    return OWNER_ALERTS.get(reason, "⚠️ Sales are paused.")
