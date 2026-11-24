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
from app.numberhub import NumberHubError, TransportError, dec

log = logging.getLogger(__name__)

OXAPAY = "oxapay"
CRYPTOBOT = "cryptobot"
NUMBERHUB = "numberhub"
KASERA = "kasera"
# The gateways an owner connects with a key of their own.
PROVIDERS = (OXAPAY, CRYPTOBOT, KASERA)
LABEL = {OXAPAY: "OxaPay", CRYPTOBOT: "@CryptoBot", NUMBERHUB: "Built-in crypto", KASERA: "KaseraPay (QRIS)"}
# Built-in payments: the coins and networks a customer can pay in. NumberHub lists
# them (GET /topups/methods: every coin its account takes to a plain address); these
# two are what an API without that list offers, and the fallback when it can't be read.
METHODS = {
    "usdt_bsc": {"coin": "USDT", "short": "BEP20", "network": "BNB Smart Chain (BEP20)", "partial": True,
                 "min": Decimal("0")},
    "usdt_tron": {"coin": "USDT", "short": "TRC20", "network": "Tron (TRC20)", "partial": True,
                  "min": Decimal("0")},
}
# The order coins are offered in: what customers pay with most, first.
COIN_ORDER = ("USDT", "BTC", "ETH", "LTC", "TRX", "BNB", "SOL", "USDC", "DOGE", "POL", "BCH", "XMR", "DAI", "SHIB")
METHODS_TTL = 600.0
_methods_cache: tuple[float, dict[str, dict]] | None = None


async def methods(reseller: Reseller) -> dict[str, dict]:
    """{method id: {coin, short, network, partial}} for this shop's customers, read
    from NumberHub and kept 10 minutes (the list is the same for every shop)."""
    global _methods_cache
    now = time.monotonic()
    if _methods_cache is not None and now - _methods_cache[0] < METHODS_TTL:
        return _methods_cache[1]
    from app import selling
    client = selling.client_for(reseller.id)
    found: dict[str, dict] = {}
    if client is not None:
        try:
            for m in await client.topup_methods():
                found[str(m["id"])] = {"coin": str(m["coin"]).upper(), "short": str(m.get("network_short") or ""),
                                       "network": str(m.get("network") or ""), "partial": bool(m.get("partial_credit")),
                                       # A coin's own minimum (Bitcoin cannot take a few dollars).
                                       "min": dec(m.get("min_amount"))}
        except NumberHubError as exc:
            # 404 = an API from before the list existed: the two USDT networks. Any
            # other trouble: the same two for now, and ask again soon.
            if exc.status != 404:
                return dict(METHODS)
    if not found:
        found = dict(METHODS)
    _methods_cache = (now, found)
    return found


def for_amount(table: dict[str, dict], amount: Decimal | None) -> dict[str, dict]:
    """The methods that can take this amount (every method when no amount is given)."""
    if amount is None:
        return table
    return {mid: m for mid, m in table.items() if m.get("min", Decimal("0")) <= amount}


def coins(table: dict[str, dict], amount: Decimal | None = None) -> list[str]:
    """The coins of a method table that can take `amount`, most used first, unknown
    ones after in A to Z."""
    have = {m["coin"] for m in for_amount(table, amount).values()}
    return [c for c in COIN_ORDER if c in have] + sorted(have - set(COIN_ORDER))
INVOICE_MINUTES = 60
# A link is still checked this long after it expired: a payment sent just before
# the end can confirm on-chain a little later.
LATE_GRACE = dt.timedelta(hours=6)
OPEN_PER_MEMBER = 3          # unpaid links one customer may have at once
PER_DAY_PER_MEMBER = 10      # links one customer may open in 24 h
PER_DAY_PER_SHOP = 300       # built-in payment addresses left unpaid that one shop may open in 24 h
# A built-in address lives 3 hours, not 60 minutes: a withdrawal from an exchange can
# take most of an hour, and money that reaches an address after it ended is lost (the
# gateway hands the address to someone else later).
BUILTIN_MINUTES = 180
# How often NumberHub is asked about one built-in payment (the customer's "I've paid"
# button and the poller together). Every ask uses the shop's own API key, whose
# request budget is also what its purchases run on.
ASK_EVERY_FRESH = 20.0       # seconds, in a payment's first 20 minutes
ASK_EVERY_OLD = 60.0         # after that, while it is open
ASK_EVERY_LATE = 3600.0      # once it was given up (a late payment is still credited)
LATE_CREDIT_DAYS = 3         # how long a given-up built-in payment is still looked at
_last_ask: dict[int, float] = {}
_member_locks: dict[int, asyncio.Lock] = {}
_last_sweep = 0.0


def member_lock(member_id: int) -> asyncio.Lock:
    """One payment is opened at a time per customer, so taps sent together cannot
    each see room under the limits."""
    return _member_locks.setdefault(member_id, asyncio.Lock())


def _ask_due(payment_id: int, every: float) -> bool:
    now = time.monotonic()
    if now - _last_ask.get(payment_id, -1e9) < every:
        return False
    _last_ask[payment_id] = now
    return True
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


class Kasera:
    """KaseraPay, Indonesia (pay.kasera.id/docs/api): POST /v1/transactions with
    payment_methods ["qris"] answers the QRIS code as text (the bot draws it);
    GET /v1/transactions/{id} its status; header Authorization: Bearer <key>.
    Amounts are whole rupiah. A kp_test_ key makes test payments: they are reported
    as working and never add balance."""
    BASE = "https://pay.kasera.id/v1"
    MIN_IDR, MAX_IDR = 1000, 1_000_000     # above 1,000,000 the gateway wants buyer and delivery details
    USUAL_RATE = (13_000, 22_000)          # rupiah for a dollar: outside it the owner is asked to check

    def __init__(self, key: str):
        self.key = key

    @property
    def test(self) -> bool:
        return self.key.startswith("kp_test_")

    async def _call(self, method: str, path: str, body: dict | None = None, idem: str | None = None) -> tuple[int, dict]:
        headers = {"Authorization": f"Bearer {self.key}", "Accept": "application/json"}
        if idem:
            headers["Idempotency-Key"] = idem
        async with _http() as c:
            try:
                r = await c.request(method, f"{self.BASE}{path}", json=body, headers=headers)
            except httpx.HTTPError as exc:
                raise PayError(f"KaseraPay could not be reached ({type(exc).__name__})") from exc
        try:
            data = r.json()
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        return r.status_code, (data["data"] if isinstance(data.get("data"), dict) else data)

    @staticmethod
    def _why(status: int, data: dict) -> str:
        err = data.get("error")
        msg = (err.get("message") or err.get("code")) if isinstance(err, dict) else (err or data.get("message"))
        return f"KaseraPay: {msg}" if msg else f"KaseraPay answered HTTP {status}"

    async def verify(self) -> str:
        """Looks up a payment that does not exist: "not found" proves the key is
        accepted, without using up one of the few payments an unverified account has."""
        status, data = await self._call("GET", "/transactions/payreq_keycheck")
        if status in (401, 403):
            raise PayError("KaseraPay rejected this key")
        if status >= 400 and status != 404:
            raise PayError(self._why(status, data))
        return "KaseraPay account" + (", TEST mode: payments add no balance" if self.test else "")

    async def create_qris(self, idr: int, ref: str, description: str) -> tuple[str, str, dt.datetime]:
        status, d = await self._call("POST", "/transactions", {
            "amount": int(idr), "payment_methods": ["qris"], "merchant_ref": ref[:64], "external_id": ref[:64],
            "description": description[:255], "expires_in_minutes": INVOICE_MINUTES}, idem=ref)
        if status >= 400:
            raise PayError(self._why(status, d))
        qr = str(((d.get("payment") or {}) if isinstance(d.get("payment"), dict) else {}).get("qr_string") or "")
        if not d.get("id") or not qr:
            raise PayError("KaseraPay gave no QRIS code")
        if int(d.get("amount") or 0) != int(idr):
            raise PayError("KaseraPay answered with another amount than was asked")
        # The gateway answers in Jakarta time ("…+07:00"). Never later than was asked
        # for: a time without a zone, read as UTC, would keep the payment open for hours.
        latest = _now() + dt.timedelta(minutes=INVOICE_MINUTES)
        until = _parse_iso(d.get("expires_at"))
        if until is None or until <= _now() or until > latest + dt.timedelta(minutes=2):
            until = latest
        return str(d["id"]), qr, until

    async def state(self, invoice_id: str) -> tuple[str, int, bool]:
        """(paid | pending | expired, the rupiah it reports, a real payment or a test one)."""
        status, d = await self._call("GET", f"/transactions/{invoice_id}")
        if status >= 400:
            raise PayError(self._why(status, d))
        s = str(d.get("status") or "").lower()
        state = "paid" if s == "succeeded" else "expired" if s in ("expired", "canceled", "failed") else "pending"
        return state, int(d.get("amount") or 0), bool(d.get("livemode"))

    async def status(self, invoice_id: str) -> str:
        return (await self.state(invoice_id))[0]


def client_for(provider: str, secret: str):
    return {OXAPAY: OxaPay, CRYPTOBOT: CryptoBot, KASERA: Kasera}[provider](secret)


def qr_png(text: str) -> bytes:
    """The picture of a QR code (a QRIS payload) for a Telegram photo."""
    import io as _io

    import segno
    buf = _io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=8, border=3)
    return buf.getvalue()


def kasera_rate(reseller: Reseller) -> Decimal | None:
    """Rupiah for one dollar in this shop, as its owner set it."""
    rate = getattr(reseller, "kasera_rate", None)
    return Decimal(rate) if rate is not None and Decimal(rate) > 0 else None


def kasera_rate_odd(rate) -> bool:
    """Far from what a dollar costs: most likely a typo, or a test left on."""
    low, high = Kasera.USUAL_RATE
    return rate is not None and not low <= Decimal(rate) <= high


def kasera_limits(reseller: Reseller) -> tuple[Decimal, Decimal] | None:
    """The smallest and largest dollar amount one QRIS payment can be at this shop's rate."""
    rate = kasera_rate(reseller)
    if rate is None:
        return None
    cent = Decimal("0.01")
    low = (Decimal(Kasera.MIN_IDR) / rate).quantize(cent, rounding="ROUND_UP")
    high = (Decimal(Kasera.MAX_IDR) / rate).quantize(cent, rounding="ROUND_DOWN")
    return max(low, cent), high


def kasera_fits(reseller: Reseller, amount: Decimal) -> bool:
    lim = kasera_limits(reseller)
    return lim is not None and lim[0] <= amount <= lim[1]


# ─── what a shop has connected ───────────────────────────────────────────────
_ENC_FIELD = {OXAPAY: "oxapay_key_enc", CRYPTOBOT: "cryptobot_token_enc", KASERA: "kasera_key_enc"}


def _enc_field(provider: str) -> str:
    return _ENC_FIELD[provider]


def has_key(reseller: Reseller, provider: str) -> bool:
    return bool(getattr(reseller, _enc_field(provider), None))


def builtin_on(reseller: Reseller) -> bool:
    """Whether this shop takes built-in USDT payments: the owner's choice, or the
    platform's default while they have not chosen."""
    choice = getattr(reseller, "nh_pay", None)
    return bool(settings.numberhub_pay_default) if choice is None else bool(choice)


def own(reseller: Reseller) -> list[str]:
    """The owner's own gateways that can take a payment (KaseraPay also needs its rupiah rate)."""
    return [p for p in PROVIDERS if has_key(reseller, p) and (p != KASERA or kasera_rate(reseller) is not None)]


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
    table = await methods(reseller)
    if method not in table:
        raise PayError("unknown network")
    if amount < table[method].get("min", Decimal("0")):
        raise PayError(f"the least for this coin is ${table[method]['min']:.2f}")
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
    cover = await backing(reseller)
    if cover is None:
        raise PayError("NumberHub could not be reached")
    wallet, owed = cover
    if wallet + Decimal("0.01") < owed:
        # The customers' money is what the shop buys their numbers with. If the wallet
        # was spent on something else, no more customers are asked to pay into it.
        log.error("reseller %s: wallet %s no longer covers customers' built-in balances %s: no new payments",
                  reseller.id, wallet, owed)
        raise PayError(f"your NumberHub wallet (${wallet:.2f}) no longer covers what your customers paid in "
                       f"and have not spent yet (${owed:.2f}). Top it up to take payments again")
    row = await repo.create_payment(reseller.id, member_id, NUMBERHUB, amount)
    ref = builtin_reference(row)
    try:
        top = await client.open_topup(amount, method, ref, BUILTIN_MINUTES)
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
    until = _parse_iso(top.get("expires_at")) or _now() + dt.timedelta(minutes=BUILTIN_MINUTES)
    if str(top.get("pay_currency") or "").upper() != table[method]["coin"]:
        await repo.close_payment(row.id, Payment.FAILED)
        log.error("reseller %s: deposit for payment %s came back in another coin", reseller.id, row.id)
        raise PayError("NumberHub answered with another coin than was asked for")
    shown = {"method": method, "address": address, "pay_amount": pay_amount,
             "pay_currency": table[method]["coin"],
             "network": str(top.get("network") or table[method]["network"]),
             "network_short": str(top.get("network_short") or table[method]["short"])}
    await repo.attach_invoice(row.id, str(top["id"]), None, until, details=json.dumps(shown))
    return await repo.get_payment(row.id)


async def backing(reseller: Reseller) -> tuple[Decimal, Decimal] | None:
    """(what the shop's NumberHub wallet holds, what its customers paid in with
    built-in payments and have not spent). None when the wallet can't be read."""
    from app import selling
    client = selling.client_for(reseller.id)
    if client is None:
        return None
    try:
        wallet = dec((await client.balance()).get("balance"))
    except NumberHubError:
        return None
    return wallet, await repo.builtin_unspent(reseller.id)


def _parse_iso(value) -> dt.datetime | None:
    """A gateway's time, as UTC. The database keeps no time zone, so a time with
    another offset must be converted before it is stored: its offset is dropped."""
    try:
        when = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)).astimezone(dt.timezone.utc)


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


async def _open_kasera(reseller: Reseller, member_id: int, amount: Decimal) -> Payment:
    """A QRIS payment in rupiah for a dollar amount, at the shop's own rate. The
    dollars the customer gets are fixed now; the rupiah are what the QR asks for."""
    key, rate = secret(reseller, KASERA), kasera_rate(reseller)
    if not key or rate is None:
        raise PayError("KaseraPay is not connected, or its rupiah rate is not set")
    if not kasera_fits(reseller, amount):
        low, high = kasera_limits(reseller)
        raise PayError(f"one QRIS payment can be from ${low} to ${high} at your rate")
    idr = int((amount * rate).to_integral_value(rounding="ROUND_UP"))
    row = await repo.create_payment(reseller.id, member_id, KASERA, amount)
    ref = f"s{reseller.id}-p{row.id}"
    client = Kasera(key)
    title = reseller.bot_title or reseller.bot_username or "shop"
    try:
        invoice_id, qr, until = await client.create_qris(idr, ref, f"Balance top-up {title}")
    except PayError:
        await repo.close_payment(row.id, Payment.FAILED)
        raise
    except Exception as exc:  # noqa: BLE001
        await repo.close_payment(row.id, Payment.FAILED)
        raise PayError("KaseraPay did not answer") from exc
    await repo.attach_invoice(row.id, invoice_id, None, until, details=json.dumps(
        {"qr": qr, "idr": idr, "rate": str(rate), "test": client.test}))
    return await repo.get_payment(row.id)


async def open_link(reseller: Reseller, member_id: int, provider: str, amount: Decimal,
                    method: str | None = None) -> Payment:
    """Create the payment row, then the gateway's invoice (or, built in, the
    deposit address on `method`'s network). Raises PayError."""
    if provider == NUMBERHUB:
        if not builtin_on(reseller):
            raise PayError("built-in payments are off")
        return await _open_builtin(reseller, member_id, amount, method)
    if provider == KASERA:
        return await _open_kasera(reseller, member_id, amount)
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
        age = _now() - _aware(payment.created_at)
        if not _ask_due(payment.id, ASK_EVERY_FRESH if age < dt.timedelta(minutes=20) else ASK_EVERY_OLD):
            return "pending"
        state, credit = await _builtin_state(reseller, payment)
        if state in ("unknown", "expired") and not late:
            # Not final yet: a transfer sent just before the end is confirmed by the
            # network afterwards, and NumberHub then turns the deposit paid.
            return "unknown" if state == "unknown" else "pending"
        if state == "unknown":
            await repo.close_payment(payment.id, Payment.EXPIRED)
            return "expired"
    else:
        key = secret(reseller, payment.provider)
        if not key:
            # The owner removed the key: nothing can confirm it any more.
            if late:
                await repo.close_payment(payment.id, Payment.EXPIRED)
                return "expired"
            return "unknown"
        try:
            if payment.provider == KASERA:
                state, idr, live = await Kasera(key).state(payment.invoice_id)
                shown = details(payment)
                if state == "paid" and idr != int(shown.get("idr") or -1):
                    log.error("reseller %s: payment %s paid with another amount (%s)", reseller.id, payment.id, idr)
                    return "unknown"
                if state == "paid" and (shown.get("test") or not live):
                    # A test key's payment: the flow works, and no balance is added.
                    if await repo.close_payment(payment.id, Payment.EXPIRED):
                        await _announce_test(bot, reseller, payment)
                    return "test"
                if state == "expired" and not late:
                    return "pending"        # KaseraPay can still turn an expired payment paid
            else:
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


async def check_late(payment: Payment, reseller: Reseller | None = None, bot=None) -> str:
    """A built-in payment that was given up, asked about once more. If NumberHub
    credited the shop's wallet after all (a slow network, or the gateway's support
    releasing a late transfer), the customer gets it too, once."""
    if payment.status != Payment.EXPIRED or payment.provider != NUMBERHUB or not payment.invoice_id:
        return payment.status
    reseller = reseller or await repo.get_reseller(payment.reseller_id)
    if reseller is None or not _ask_due(payment.id, ASK_EVERY_LATE):
        return "expired"
    state, credit = await _builtin_state(reseller, payment)
    if state != "paid":
        return "expired"
    done = await repo.credit_payment(payment.id, credit, late=True)
    if done is None:
        fresh = await repo.get_payment(payment.id)
        return fresh.status if fresh else "expired"
    log.warning("reseller %s: payment %s was paid after it was given up: credited %s", reseller.id, payment.id, credit)
    await _announce_paid(bot, reseller, done)
    return "paid"


async def check_more(payment: Payment, reseller: Reseller | None = None, bot=None) -> Decimal:
    """A built-in payment that was already credited: did more arrive on it? When a
    customer sends a second transfer to the same address, NumberHub adds it to the
    shop's wallet and raises the deposit's credited amount. The customer gets the
    difference here, once. Returns what was added."""
    zero = Decimal("0")
    if payment.status != Payment.PAID or payment.provider != NUMBERHUB or not payment.invoice_id:
        return zero
    reseller = reseller or await repo.get_reseller(payment.reseller_id)
    if reseller is None or not _ask_due(payment.id, ASK_EVERY_LATE):
        return zero
    state, credit = await _builtin_state(reseller, payment)
    have = Decimal(payment.credited or 0)
    if state != "paid" or credit is None or credit - have < Decimal("0.01"):
        return zero
    added = await repo.credit_payment_more(payment.id, have, credit)
    if not added:
        return zero
    log.warning("reseller %s: payment %s received more after it was credited: +%s", reseller.id, payment.id, added)
    from app.i18n import t
    from app.selling import send
    from app.texts import esc, money
    bot = _bot(reseller.id, bot)
    member = await repo.get_member(payment.member_id)
    if bot is not None and member is not None:
        await send(bot, member.telegram_id, t(member.language or "en", "pay_received", amount=money(added),
                                              balance=money(member.available)))
        who = f"<code>{member.telegram_id}</code>" + (f" @{esc(member.username)}" if member.username else "")
        await send(bot, reseller.owner_id,
                   f"💰 <b>More arrived on a payment:</b> +{money(added)} from {who} in "
                   f"{esc(details(payment).get('pay_currency') or 'crypto')}.{chr(10)}"
                   f"It was added to your NumberHub wallet and to their shop balance, now "
                   f"<b>{money(member.available)}</b>.")
    return added


async def sweep_expired(force: bool = False) -> int:
    """Every 10 minutes: the built-in payments given up in the last days, in case
    one was paid late. Returns how many were credited."""
    global _last_sweep
    if not force and time.monotonic() - _last_sweep < 600:
        return 0
    _last_sweep = time.monotonic()
    credited = 0
    for row in await repo.expired_builtin_payments(NUMBERHUB, LATE_CREDIT_DAYS):
        try:
            if await check_late(row) == "paid":
                credited += 1
        except Exception:  # noqa: BLE001
            log.exception("late check of payment %s crashed", row.id)
    for row in await repo.paid_builtin_payments(NUMBERHUB, LATE_CREDIT_DAYS):
        try:
            if await check_more(row) > 0:
                credited += 1
        except Exception:  # noqa: BLE001
            log.exception("second look at payment %s crashed", row.id)
    return credited


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
        coin = esc(details(p).get("pay_currency") or "crypto")
        await send(bot, reseller.owner_id,
                   f"💰 <b>Payment received:</b> +{money(p.credited)} from {who} in {coin}.\n"
                   f"The money was added to your NumberHub wallet, so your shop can sell with it; their "
                   f"shop balance is now <b>{money(member.available)}</b>.")
        return
    await send(bot, reseller.owner_id,
               f"💰 <b>Payment received:</b> +{money(p.credited)} from {who} via {LABEL[p.provider]}.\n"
               f"The money is in your {LABEL[p.provider]} balance; their shop balance is now "
               f"<b>{money(member.available)}</b>.")


async def _announce_test(bot, reseller: Reseller, p: Payment) -> None:
    from app.selling import send
    from app.texts import money
    bot = _bot(reseller.id, bot)
    member = await repo.get_member(p.member_id)
    if bot is None or member is None:
        return
    note = (f"✅ <b>Test payment worked</b> ({money(p.amount)} by QRIS, KaseraPay test mode). No balance was "
            "added: connect your live key in ⚙️ Admin panel → 💳 Payments to take real payments.")
    await send(bot, reseller.owner_id, note)
    if member.telegram_id != reseller.owner_id:
        await send(bot, member.telegram_id, "✅ Test payment received. This shop's QRIS is in test mode, so no "
                                            "balance was added.")


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
    return credited + await sweep_expired()
