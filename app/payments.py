"""Automatic top-ups with the shop owner's OWN payment account.

The owner connects an OxaPay merchant API key and/or a @CryptoBot (Crypto Pay)
app token in ⚙️ Admin → 💳 Payments. A customer picks an amount, pays the link,
and the payment poller (main.payments_loop) credits their shop balance once the
gateway says the invoice is paid. The money lands in the owner's own OxaPay or
@CryptoBot balance: this platform never holds it, so there is nothing to pay out.

Crediting happens in repo.credit_payment only (one conditional UPDATE from
pending, in the same transaction as the balance change), so the poller, the
customer's "I've paid" button and a restart can race and still credit once.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from decimal import Decimal

import httpx

from app import crypto, repo
from app.models import Payment, Reseller

log = logging.getLogger(__name__)

OXAPAY = "oxapay"
CRYPTOBOT = "cryptobot"
PROVIDERS = (OXAPAY, CRYPTOBOT)
LABEL = {OXAPAY: "OxaPay", CRYPTOBOT: "@CryptoBot"}
INVOICE_MINUTES = 60
# A link is still checked this long after it expired: a payment sent just before
# the end can confirm on-chain a little later.
LATE_GRACE = dt.timedelta(hours=6)
OPEN_PER_MEMBER = 3          # unpaid links one customer may have at once
PER_DAY_PER_MEMBER = 10      # links one customer may open in 24 h
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


def connected(reseller: Reseller) -> list[str]:
    return [p for p in PROVIDERS if getattr(reseller, _enc_field(p), None)]


def secret(reseller: Reseller, provider: str) -> str | None:
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
    await send(bot, reseller.owner_id,
               f"⚠️ <b>{LABEL[provider]} refused to make a payment link</b> for a customer: {esc(why)}\n\n"
               "Check your key in ⚙️ Admin panel → 💳 Payments.")


async def open_link(reseller: Reseller, member_id: int, provider: str, amount: Decimal) -> Payment:
    """Create the payment row, then the gateway's invoice. Raises PayError."""
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
    key = secret(reseller, payment.provider)
    late = payment.expires_at is not None and _aware(payment.expires_at) + LATE_GRACE < _now()
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
        done = await repo.credit_payment(payment.id)
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
