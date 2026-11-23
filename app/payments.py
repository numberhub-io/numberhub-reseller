"""Automatic top-ups: the shop owner's OWN payment account, or built in.

Own account: the owner connects an OxaPay merchant API key and/or a @CryptoBot
(Crypto Pay) app token in ⚙️ Admin → 💳 Payments. A customer picks an amount,
pays the link, and the payment poller (main.payments_loop) credits their shop
balance once the gateway says the invoice is paid. The money lands in the owner's
own OxaPay or @CryptoBot balance: this platform never holds it.

Built in (provider "numberhub"): nothing to connect. The shop asks NumberHub,
with its own API key, for a USDT deposit address for this one customer
(POST /topups with a reference). The customer sends USDT to it; NumberHub adds
the money to the SHOP's wallet, and the poller then adds the same amount to the
customer's balance here. So the shop's customers fund the wallet their numbers
are bought from. This platform never holds that money either.

Crediting happens in repo.credit_payment only (one conditional UPDATE from
pending, in the same transaction as the balance change), so the poller, the
customer's "I've paid" button and a restart can race and still credit once.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from decimal import Decimal

import httpx

from app import crypto, repo
from app.config import settings
from app.models import Payment, Reseller
from app.numberhub import NumberHubError, TransportError

log = logging.getLogger(__name__)

OXAPAY = "oxapay"
CRYPTOBOT = "cryptobot"
NUMBERHUB = "numberhub"
# The gateways an owner connects with a key of their own.
PROVIDERS = (OXAPAY, CRYPTOBOT)
LABEL = {OXAPAY: "OxaPay", CRYPTOBOT: "@CryptoBot", NUMBERHUB: "Built-in USDT"}
# Built-in payments: the networks a customer can send USDT on (NumberHub's
# `method` values) and what the customer is shown for each.
METHODS = {
    "usdt_bsc": {"short": "BEP20", "network": "BNB Smart Chain (BEP20)"},
    "usdt_tron": {"short": "TRC20", "network": "Tron (TRC20)"},
}
INVOICE_MINUTES = 60
# A link is still checked this long after it expired: a payment sent just before
# the end can confirm on-chain a little later.
LATE_GRACE = dt.timedelta(hours=6)
OPEN_PER_MEMBER = 3          # unpaid links one customer may have at once
PER_DAY_PER_MEMBER = 10      # links one customer may open in 24 h
PER_DAY_PER_SHOP = 300       # built-in payment addresses one shop may open in 24 h
MAX_AMOUNT = Decimal("1000")
PRESETS = (Decimal("5"), Decimal("10"), Decimal("20"), Decimal("50"))
# Tests swap in an httpx.MockTransport here.
TRANSPORT: httpx.AsyncBaseTransport | None = None


class PayError(Exception):
    """The gateway refused or could not be reached. str() is safe to show the owner."""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _http() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20.0, transport=TRANSPORT)


# ─── gateways ────────────────────────────────────────────────────────────────
class OxaPay:
    """docs.oxapay.com: POST /v1/payment/invoice, GET /v1/payment/{track_id};
    header merchant_api_key."""
    BASE = "https://api.oxapay.com/v1"
    _PAID = {"paid", "completed", "confirmed"}
    _GONE = {"expired", "failed", "canceled", "cancelled"}
    _REFUND = {"refunded", "refunding"}

    def __init__(self, key: str):
        self.key = key

    async def _post(self, path: str, body: dict) -> dict:
        async with _http() as c:
            try:
                r = await c.post(f"{self.BASE}{path}", json=body,
                                 headers={"merchant_api_key": self.key, "Content-Type": "application/json"})
            except httpx.HTTPError as exc:
                raise PayError(f"OxaPay could not be reached ({type(exc).__name__})") from exc
        return self._unwrap(r)

    @staticmethod
    def _unwrap(r: httpx.Response) -> dict:
        try:
            data = r.json()
        except ValueError as exc:
            raise PayError(f"OxaPay answered HTTP {r.status_code}") from exc
        err = data.get("error") if isinstance(data, dict) else None
        if err:
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise PayError(f"OxaPay: {msg or 'request refused'}")
        if r.status_code >= 400:
            raise PayError(f"OxaPay answered HTTP {r.status_code}")
        return (data.get("data") or {}) if isinstance(data, dict) else {}

    async def create(self, amount: Decimal, order_id: str, description: str,
                     sandbox: bool = False) -> tuple[str, str, dt.datetime]:
        d = await self._post("/payment/invoice", {
            "amount": float(amount), "currency": "USD", "lifetime": INVOICE_MINUTES,
            # The customer pays the gateway fee, so the owner gets the full amount.
            "fee_paid_by_payer": 1,
            # A short payment can be completed with a second one; it is credited
            # only when the whole amount arrived ("paid").
            "mixed_payment": 1,
            "order_id": order_id, "description": description[:100], "sandbox": sandbox,
        })
        track, url = str(d.get("track_id") or ""), str(d.get("payment_url") or "")
        if not track or not url:
            raise PayError("OxaPay gave no payment link")
        exp = d.get("expired_at")
        until = (dt.datetime.fromtimestamp(int(exp), dt.timezone.utc) if str(exp or "").isdigit()
                 else _now() + dt.timedelta(minutes=INVOICE_MINUTES))
        return track, url, until

    async def status(self, invoice_id: str) -> str:
        async with _http() as c:
            try:
                r = await c.get(f"{self.BASE}/payment/{invoice_id}", headers={"merchant_api_key": self.key})
            except httpx.HTTPError as exc:
                raise PayError(f"OxaPay could not be reached ({type(exc).__name__})") from exc
        s = str(self._unwrap(r).get("status") or "").lower()
        if s in self._PAID:
            return "paid"
        if s == "manual_accept":
            # The owner accepted a short payment in their OxaPay dashboard: we don't
            # know how much arrived, so the owner adds it by hand.
            return "manual"
        if s in self._REFUND:
            return "refunded"
        if s in self._GONE:
            return "expired"
        return "pending"

    async def verify(self) -> str:
        """A sandbox invoice: proves the key works without any real payment."""
        await self.create(Decimal("1"), "key-check", "Key check", sandbox=True)
        return "OxaPay merchant"


class CryptoBot:
    """help.crypt.bot/crypto-pay-api: createInvoice / getInvoices / getMe; header
    Crypto-Pay-API-Token. The invoice is in USD and the customer picks the coin."""
    BASE = "https://pay.crypt.bot/api"

    def __init__(self, token: str):
        self.token = token

    async def _call(self, method: str, **params) -> dict:
        async with _http() as c:
            try:
                r = await c.post(f"{self.BASE}/{method}", headers={"Crypto-Pay-API-Token": self.token},
                                 json={k: v for k, v in params.items() if v is not None})
            except httpx.HTTPError as exc:
                raise PayError(f"@CryptoBot could not be reached ({type(exc).__name__})") from exc
        try:
            data = r.json()
        except ValueError as exc:
            raise PayError(f"@CryptoBot answered HTTP {r.status_code}") from exc
        if not data.get("ok"):
            err = data.get("error") or {}
            raise PayError(f"@CryptoBot: {err.get('name') or err.get('code') or 'request refused'}")
        return data.get("result") or {}

    async def create(self, amount: Decimal, order_id: str, description: str) -> tuple[str, str, dt.datetime]:
        inv = await self._call("createInvoice", currency_type="fiat", fiat="USD", amount=f"{amount:.2f}",
                               description=description[:1024], payload=order_id, allow_comments=False,
                               allow_anonymous=True, expires_in=INVOICE_MINUTES * 60)
        url = (inv.get("bot_invoice_url") or inv.get("mini_app_invoice_url") or inv.get("web_app_invoice_url")
               or inv.get("pay_url") or "")
        if not inv.get("invoice_id") or not url:
            raise PayError("@CryptoBot gave no payment link")
        return str(inv["invoice_id"]), str(url), _now() + dt.timedelta(minutes=INVOICE_MINUTES)

    async def status(self, invoice_id: str) -> str:
        res = await self._call("getInvoices", invoice_ids=str(invoice_id))
        items = res.get("items") or []
        s = str((items[0] if items else {}).get("status") or "").lower()
        if s == "paid":
            return "paid"
        if s == "expired":
            return "expired"
        return "pending"

    async def verify(self) -> str:
        me = await self._call("getMe")
        return str(me.get("name") or "Crypto Pay app")


def client_for(provider: str, secret: str):
    return OxaPay(secret) if provider == OXAPAY else CryptoBot(secret)


# ─── what a shop has connected ───────────────────────────────────────────────
def _enc_field(provider: str) -> str:
    return "oxapay_key_enc" if provider == OXAPAY else "cryptobot_token_enc"


def builtin_on(reseller: Reseller) -> bool:
    """Whether this shop takes built-in USDT payments: the owner's choice, or the
    platform's default while they have not chosen."""
    choice = getattr(reseller, "nh_pay", None)
    return bool(settings.numberhub_pay_default) if choice is None else bool(choice)


def own(reseller: Reseller) -> list[str]:
    """The owner's own gateways that are connected."""
    return [p for p in PROVIDERS if getattr(reseller, _enc_field(p), None)]


def connected(reseller: Reseller) -> list[str]:
    """Every way this shop's customers can pay automatically."""
    return own(reseller) + ([NUMBERHUB] if builtin_on(reseller) else [])


async def set_builtin(reseller_id: int, on: bool) -> None:
    await repo.update_reseller(reseller_id, nh_pay=1 if on else 0)


def secret(reseller: Reseller, provider: str) -> str | None:
    if provider not in PROVIDERS:
        return None
    enc = getattr(reseller, _enc_field(provider), None)
    if not enc:
        return None
    try:
        return crypto.decrypt(enc)
    except crypto.SecretKeyMissing:
        log.error("reseller %s: stored %s secret can't be decrypted", reseller.id, provider)
        return None


async def connect(reseller_id: int, provider: str, raw: str) -> str:
    """Check the key with the gateway, then store it encrypted. Returns what the
    gateway calls the account; raises PayError when it is refused."""
    raw = raw.strip()
    if not 8 <= len(raw) <= 200 or any(ch.isspace() for ch in raw):
        raise PayError("that doesn't look like a key")
    name = await client_for(provider, raw).verify()
    await repo.update_reseller(reseller_id, **{_enc_field(provider): crypto.encrypt(raw)})
    return name


async def disconnect(reseller_id: int, provider: str) -> None:
    await repo.update_reseller(reseller_id, **{_enc_field(provider): None})


# ─── one payment ─────────────────────────────────────────────────────────────
_owner_warned: dict[tuple[int, str], float] = {}


async def warn_owner_once(bot, reseller: Reseller, provider: str, why: str) -> None:
    """A link could not be made (key revoked, account blocked): tell the owner, at
    most once an hour per gateway, so customers aren't left guessing."""
    key = (reseller.id, provider)
    if time.monotonic() - _owner_warned.get(key, -1e9) < 3600 or bot is None:
        return
    _owner_warned[key] = time.monotonic()
    from app.selling import send
    from app.texts import esc
    if provider == NUMBERHUB:
        await send(bot, reseller.owner_id,
                   f"⚠️ <b>A customer could not get a payment address</b>: {esc(why)}\n\n"
                   "Built-in payments use your NumberHub API key. Check it in 🤖 Your own bot, "
                   "or turn them off in ⚙️ Admin panel → 💳 Payments.")
        return
    await send(bot, reseller.owner_id,
               f"⚠️ <b>{LABEL[provider]} refused to make a payment link</b> for a customer: {esc(why)}\n\n"
               "Check your key in ⚙️ Admin panel → 💳 Payments.")


def details(payment: Payment) -> dict:
    """What a built-in payment shows instead of a link ({} for a link payment)."""
    try:
        d = json.loads(payment.details) if getattr(payment, "details", None) else {}
    except (TypeError, ValueError):
        d = {}
    return d if isinstance(d, dict) else {}


def builtin_reference(payment: Payment) -> str:
    """Who a built-in deposit is for, as NumberHub stores it: this shop, this
    customer, this payment row. No name, no Telegram id."""
    return f"s{payment.reseller_id}-m{payment.member_id}-p{payment.id}"


def _builtin_refusal(exc: NumberHubError) -> str:
    """NumberHub's answer in words the owner can act on."""
    if isinstance(exc, TransportError):
        return "NumberHub could not be reached"
    if exc.status in (401, 403):
        return "NumberHub refused the shop's API key (revoked, or it may not buy)"
    if exc.code == "too_many_open_topups":
        return "too many customers have a payment address open right now; they free up within the hour"
    if exc.code == "amount_out_of_range":
        return f"the amount is outside NumberHub's limits (from {exc.data.get('min', '1.00')})"
    if exc.code in ("invalid_reference", "invalid_lifetime"):
        return "this NumberHub API does not take per-customer deposits yet"
    return f"NumberHub answered {exc.code}"


async def _open_builtin(reseller: Reseller, member_id: int, amount: Decimal, method: str | None) -> Payment:
    """One USDT deposit address for ONE customer, opened with the shop's own
    NumberHub key. The reference is unique to this payment row, and the answer
    must repeat it: an API that ignored it could hand two customers one address,
    and one payment can only be credited to one of them."""
    from app import selling
    if method not in METHODS:
        raise PayError("unknown network")
    if reseller.status != Reseller.ACTIVE:
        # A paused, suspended or disconnected shop sells nothing: it must not take
        # a customer's money either.
        raise PayError("the shop is not selling right now")
    client = selling.client_for(reseller.id)
    if client is None:
        raise PayError("the shop is not connected to NumberHub")
    if await repo.payments_today_shop(reseller.id, NUMBERHUB) >= PER_DAY_PER_SHOP:
        # Each address is a live deposit at the gateway: a script with many Telegram
        # accounts must not be able to open them without end.
        raise PayError("this shop opened too many payment addresses today")
    row = await repo.create_payment(reseller.id, member_id, NUMBERHUB, amount)
    ref = builtin_reference(row)
    try:
        top = await client.open_topup(amount, method, ref, INVOICE_MINUTES)
    except NumberHubError as exc:
        await repo.close_payment(row.id, Payment.FAILED)
        raise PayError(_builtin_refusal(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        await repo.close_payment(row.id, Payment.FAILED)
        raise PayError("NumberHub did not answer") from exc
    address, pay_amount = str(top.get("address") or "").strip(), str(top.get("pay_amount") or "").strip()
    if (top.get("reference") != ref or not top.get("id") or not address or not pay_amount
            or top.get("method") != method or str(top.get("amount")) != f"{amount:.2f}"
            or str(top.get("status") or "").lower() != "pending"):
        await repo.close_payment(row.id, Payment.FAILED)
        log.error("reseller %s: deposit for payment %s came back without its reference or address",
                  reseller.id, row.id)
        raise PayError("this NumberHub API does not take per-customer deposits yet")
    until = _parse_iso(top.get("expires_at")) or _now() + dt.timedelta(minutes=INVOICE_MINUTES)
    shown = {"method": method, "address": address, "pay_amount": pay_amount,
             "pay_currency": str(top.get("pay_currency") or "USDT"),
             "network": str(top.get("network") or METHODS[method]["network"]),
             "network_short": str(top.get("network_short") or METHODS[method]["short"])}
    await repo.attach_invoice(row.id, str(top["id"]), None, until, details=json.dumps(shown))
    return await repo.get_payment(row.id)


def _parse_iso(value) -> dt.datetime | None:
    try:
        when = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


async def _builtin_state(reseller: Reseller, payment: Payment) -> tuple[str, Decimal | None]:
    """(paid | pending | expired | unknown, what NumberHub added to the shop's
    wallet). The customer is credited exactly that amount, never the asked one: a
    short or an over payment reaches the wallet as what really arrived."""
    from app import selling
    client = selling.client_for(reseller.id)
    if client is None or not str(payment.invoice_id or "").isdigit():
        return "unknown", None
    try:
        top = await client.topup(int(payment.invoice_id))
    except NumberHubError as exc:
        log.warning("reseller %s: payment %s check failed: %s", reseller.id, payment.id, exc)
        return "unknown", None
    if top.get("reference") != builtin_reference(payment):
        # Not the deposit that was opened for this payment (for example the owner's
        # own top-up with the same number): never credit a customer from it.
        log.error("reseller %s: payment %s points at a deposit with another reference",
                  reseller.id, payment.id)
        return "unknown", None
    status = str(top.get("status") or "").lower()
    if status == "paid":
        try:
            credited = Decimal(str(top.get("credited"))).quantize(Decimal("0.01"))
        except Exception:  # noqa: BLE001
            return "unknown", None
        return ("paid", credited) if credited > 0 else ("unknown", None)
    if status == "expired":
        return "expired", None
    return "pending", None


async def open_link(reseller: Reseller, member_id: int, provider: str, amount: Decimal,
                    method: str | None = None) -> Payment:
    """Create the payment row, then the gateway's invoice (or, built in, the
    deposit address on `method`'s network). Raises PayError."""
    if provider == NUMBERHUB:
        if not builtin_on(reseller):
            raise PayError("built-in payments are off")
        return await _open_builtin(reseller, member_id, amount, method)
    key = secret(reseller, provider)
    if not key:
        raise PayError(f"{LABEL[provider]} is not connected")
    row = await repo.create_payment(reseller.id, member_id, provider, amount)
    title = reseller.bot_title or reseller.bot_username or "shop"
    try:
        invoice_id, url, until = await client_for(provider, key).create(
            amount, f"s{reseller.id}-p{row.id}", f"Balance top-up · {title}")
    except PayError:
        await repo.close_payment(row.id, Payment.FAILED)
        raise
    except Exception as exc:  # noqa: BLE001
        await repo.close_payment(row.id, Payment.FAILED)
        raise PayError(f"{LABEL[provider]} did not answer") from exc
    await repo.attach_invoice(row.id, invoice_id, url, until)
    return await repo.get_payment(row.id)


async def check(payment: Payment, reseller: Reseller | None = None, bot=None) -> str:
    """Ask the gateway about one link and act on it. Returns paid | pending |
    expired | manual | refunded | failed | unknown (couldn't ask)."""
    if payment.status != Payment.PENDING:
        return payment.status
    reseller = reseller or await repo.get_reseller(payment.reseller_id)
    if reseller is None or not payment.invoice_id:
        return "unknown"
    late = payment.expires_at is not None and _aware(payment.expires_at) + LATE_GRACE < _now()
    credit: Decimal | None = None
    if payment.provider == NUMBERHUB:
        # Asked even when the owner has since turned built-in payments off: the
        # customer may already have sent the money to the shop's wallet.
        state, credit = await _builtin_state(reseller, payment)
        if state == "unknown":
            if late:
                await repo.close_payment(payment.id, Payment.EXPIRED)
                return "expired"
            return "unknown"
    else:
        key = secret(reseller, payment.provider)
        if not key:
            # The owner removed the key: nothing can confirm it any more.
            if late:
                await repo.close_payment(payment.id, Payment.EXPIRED)
                return "expired"
            return "unknown"
        try:
            state = await client_for(payment.provider, key).status(payment.invoice_id)
        except PayError as exc:
            log.warning("reseller %s: payment %s check failed: %s", reseller.id, payment.id, exc)
            return "unknown"
    if state == "paid":
        done = await repo.credit_payment(payment.id, credit)
        if done is not None:
            await _announce_paid(bot, reseller, done)
            return "paid"
        fresh = await repo.get_payment(payment.id)
        return fresh.status if fresh else "unknown"
    if state == "manual":
        if await repo.close_payment(payment.id, Payment.MANUAL):
            await _announce_manual(bot, reseller, payment)
        return "manual"
    if state in ("expired", "refunded"):
        await repo.close_payment(payment.id, Payment.EXPIRED)
        return "expired"
    if late:
        await repo.close_payment(payment.id, Payment.EXPIRED)
        return "expired"
    return "pending"


def _aware(v: dt.datetime) -> dt.datetime:
    return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)


def _bot(reseller_id: int, bot=None):
    if bot is not None:
        return bot
    from app import runtime
    return runtime.bot_for(reseller_id)


async def _announce_paid(bot, reseller: Reseller, p: Payment) -> None:
    from app.i18n import t
    from app.selling import send
    from app.texts import esc, money
    bot = _bot(reseller.id, bot)
    member = await repo.get_member(p.member_id)
    if bot is None or member is None:
        return
    await send(bot, member.telegram_id, t(member.language or "en", "pay_received", amount=money(p.credited),
                                          balance=money(member.available)))
    who = f"<code>{member.telegram_id}</code>" + (f" @{esc(member.username)}" if member.username else "")
    if p.provider == NUMBERHUB:
        await send(bot, reseller.owner_id,
                   f"💰 <b>Payment received:</b> +{money(p.credited)} from {who} in USDT.\n"
                   f"The money was added to your NumberHub wallet, so your shop can sell with it; their "
                   f"shop balance is now <b>{money(member.available)}</b>.")
        return
    await send(bot, reseller.owner_id,
               f"💰 <b>Payment received:</b> +{money(p.credited)} from {who} via {LABEL[p.provider]}.\n"
               f"The money is in your {LABEL[p.provider]} balance; their shop balance is now "
               f"<b>{money(member.available)}</b>.")


async def _announce_manual(bot, reseller: Reseller, p: Payment) -> None:
    from app.selling import send
    from app.texts import money
    bot = _bot(reseller.id, bot)
    member = await repo.get_member(p.member_id)
    if bot is None or member is None:
        return
    await send(bot, reseller.owner_id,
               f"💳 You accepted a short payment in {LABEL[p.provider]} for a {money(p.amount)} link of "
               f"<code>{member.telegram_id}</code>. It was not added automatically: add what really arrived "
               "with ⚙️ Admin panel → ➕ Add balance.")


async def poll_all(parallel: int = 6) -> int:
    """Every open link, a few at a time. Returns how many were credited."""
    rows = await repo.pending_payments()
    gate = asyncio.Semaphore(parallel)
    credited = 0
    shops: dict[int, Reseller | None] = {}

    async def one(p: Payment):
        nonlocal credited
        async with gate:
            if p.reseller_id not in shops:
                shops[p.reseller_id] = await repo.get_reseller(p.reseller_id)
            try:
                if await check(p, shops[p.reseller_id]) == "paid":
                    credited += 1
            except Exception:  # noqa: BLE001 — one link's trouble never stops the rest
                log.exception("payment %s check crashed", p.id)

    await asyncio.gather(*(one(p) for p in rows))
    return credited
