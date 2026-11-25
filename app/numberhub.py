"""NumberHub public API client (https://api.numberhub.io/v1, Bearer API key).

The reseller's own API key is used, so every number is bought on — and paid
from — the reseller's NumberHub wallet at the price NumberHub gives them.
Money comes back as 2-dp strings; errors as {"error": code, ...}.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from decimal import Decimal
from urllib.parse import quote

import httpx

from app.config import settings

log = logging.getLogger(__name__)


class NumberHubError(Exception):
    def __init__(self, code: str, status: int = 0, data: dict | None = None):
        super().__init__(f"{code} ({status})")
        self.code = code
        self.status = status
        self.data = data or {}


class TransportError(NumberHubError):
    """The request may or may not have reached NumberHub (timeout, reset)."""


# Answers that come from NumberHub's layers in front of the purchase handler
# (API key, rate limit, the idempotency gate itself). They say nothing about
# whether a purchase sent earlier with the same key went through. Every other
# 4xx is the handler's own answer, which NumberHub stores and replays for the key.
NOT_AUTHORITATIVE_STATUS = (401, 403, 429)
NOT_AUTHORITATIVE_CODES = ("idempotency_in_progress", "idempotency_conflict", "rate_limited",
                           "invalid_idempotency_key", "idempotency_key_required")


# 5xx answers a purchase handler gives BEFORE it does anything (its price list could
# not be read just now). Unlike every other 5xx they prove that nothing was bought.
NOTHING_DONE_5XX = ("rent_unavailable",)


def authoritative(exc: NumberHubError) -> bool:
    """True when the error proves no order exists under the purchase's key."""
    if isinstance(exc, TransportError):
        return False
    return exc.status not in NOT_AUTHORITATIVE_STATUS and exc.code not in NOT_AUTHORITATIVE_CODES


def flag(iso2: str | None) -> str:
    iso2 = (iso2 or "").upper()
    if len(iso2) != 2 or not iso2.isalpha():
        return "🌐"
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in iso2)


def dec(value) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")


class NumberHub:
    def __init__(self, api_key: str, base: str | None = None, http: httpx.AsyncClient | None = None):
        self._key = api_key
        self._base = (base or settings.numberhub_api).rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))
        self._own_http = http is None

    async def close(self) -> None:
        if self._own_http:
            await self._http.aclose()

    def uses(self, api_key: str) -> bool:
        """Whether this client sends `api_key` (constant time; the key is never exposed)."""
        return hmac.compare_digest(self._key.encode(), (api_key or "").encode())

    async def _send(self, method: str, path: str, *, params=None, content: bytes | None = None,
                    headers=None) -> httpx.Response:
        hdrs = {"Authorization": f"Bearer {self._key}", "Accept": "application/json", **(headers or {})}
        if content is not None:
            hdrs["Content-Type"] = "application/json"
        try:
            r = await self._http.request(method, self._base + path, params=params, content=content, headers=hdrs)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise TransportError("transport", 0, {"detail": str(exc)}) from exc
        if r.status_code >= 500:
            try:
                code = r.json().get("error")
            except (ValueError, AttributeError):
                code = None
            if code in NOTHING_DONE_5XX:
                raise NumberHubError(code, r.status_code)
            # The server may have acted before failing: treat like a lost reply.
            raise TransportError(f"http_{r.status_code}", r.status_code)
        if r.status_code >= 400:
            try:
                data = r.json()
            except ValueError:
                data = {}
            data = data if isinstance(data, dict) else {}
            raise NumberHubError(data.get("error") or f"http_{r.status_code}", r.status_code, data)
        return r

    async def _req(self, method: str, path: str, **kw) -> dict:
        r = await self._send(method, path, **kw)
        try:
            data = r.json()
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    # ── account / catalog ────────────────────────────────────────────────────
    async def balance(self) -> dict:
        return await self._req("GET", "/balance")

    async def services(self) -> list[dict]:
        return (await self._req("GET", "/services")).get("services", [])

    async def countries(self, service: str) -> list[dict]:
        return (await self._req("GET", "/countries", params={"service": service})).get("countries", [])

    # ── numbers ──────────────────────────────────────────────────────────────
    @staticmethod
    def buy_body(service: str, country: str, max_price: Decimal) -> bytes:
        """The exact bytes NumberHub hashes for the Idempotency-Key. Serialised
        here, not by httpx, so a replay (even after an upgrade) is identical."""
        return json.dumps({"service": service, "country": str(country), "max_price": f"{max_price:.2f}"},
                          separators=(",", ":"), sort_keys=True).encode()

    async def buy(self, service: str, country: str, max_price: Decimal, idempotency_key: str) -> dict:
        """Buy a number. The Idempotency-Key makes a retry after a lost reply
        return the same order instead of buying a second one.

        Raises NumberHubError only when NumberHub's purchase handler answered (no
        order exists under the key). When an attempt may have reached NumberHub
        and the last answer proves nothing (429, auth, the idempotency gate, a
        timeout), raises TransportError: the outcome is unknown, and the caller
        keeps the hold and lets recovery ask again with the same key."""
        return await self._purchase("/numbers", self.buy_body(service, country, max_price), idempotency_key, "number")

    async def _purchase(self, path: str, body: bytes, idempotency_key: str, field: str) -> dict:
        """One purchase, asked up to three times with the same key and the same bytes.
        See buy() for what each kind of failure means."""
        last: NumberHubError | None = None
        maybe_sent = False
        for attempt in range(3):
            try:
                r = await self._send("POST", path, content=body,
                                     headers={"Idempotency-Key": idempotency_key})
                try:
                    return r.json()[field]
                except (KeyError, TypeError, ValueError) as exc:
                    # Accepted, with no order in the answer: the purchase may have
                    # happened. Asked again with the same key, like a lost reply.
                    raise TransportError("bad_reply", r.status_code) from exc
            except TransportError as exc:
                last, maybe_sent = exc, True
                await asyncio.sleep(1.5 * (attempt + 1))
            except NumberHubError as exc:
                if authoritative(exc):
                    raise
                last = exc
                if exc.code == "idempotency_in_progress":
                    maybe_sent = True          # NumberHub is working on this key right now
                elif not maybe_sent:
                    raise                      # rejected up front: nothing was bought
                await asyncio.sleep(2 if exc.status != 429 else 3)
        assert last is not None
        if isinstance(last, TransportError):
            raise last
        raise TransportError(last.code, last.status, last.data)

    async def number(self, nh_id: int) -> dict:
        return (await self._req("GET", f"/numbers/{nh_id}"))["number"]

    async def cancel(self, nh_id: int) -> dict:
        return await self._req("DELETE", f"/numbers/{nh_id}")

    async def order_of(self, nh_id: int, kind: str = "sms") -> dict:
        """One order of this account, asked for as what it is. Each kind has its own
        address, and a mailbox or a rented number asked about as a number answers "not
        found" even on its own account. 404 = this account has no such order."""
        path = {"email": "emails", "rent": "rentals", "proxy": "proxies"}.get(kind, "numbers")
        return await self._req("GET", f"/{path}/{int(nh_id)}")

    async def orders(self, limit: int = 100) -> list[dict]:
        return (await self._req("GET", "/orders", params={"limit": limit})).get("orders", [])

    # ── email codes: a mailbox that receives one mail from one site ──────────
    async def email_sites(self) -> list[dict]:
        """The sites most asked for: [{site, name}]."""
        rows = (await self._req("GET", "/email/sites")).get("sites", [])
        return [r for r in rows if isinstance(r, dict) and r.get("site")] if isinstance(rows, list) else []

    async def email_suggest(self, q: str) -> list[dict]:
        """Sites matching what a customer typed: [{site, name, known}]."""
        rows = (await self._req("GET", "/email/suggest", params={"q": q})).get("suggestions", [])
        return [r for r in rows if isinstance(r, dict) and r.get("site")] if isinstance(rows, list) else []

    async def email_domains(self, site: str) -> list[dict]:
        """Mail domains for one site, the reliable ones first:
        [{domain, price, in_stock, delivery: "high" | "low" | null}]."""
        rows = (await self._req("GET", f"/email/sites/{quote(site, safe='')}/domains")).get("domains", [])
        return [r for r in rows if isinstance(r, dict) and r.get("domain")] if isinstance(rows, list) else []

    @staticmethod
    def email_body(site: str, domain: str, max_price: Decimal) -> bytes:
        return json.dumps({"site": site, "domain": domain, "max_price": f"{max_price:.2f}"},
                          separators=(",", ":"), sort_keys=True).encode()

    async def buy_email(self, site: str, domain: str, max_price: Decimal, idempotency_key: str) -> dict:
        """A mailbox for one mail from `site`. Same rules as buy(): the key makes a
        retry return the same order, and an unclear answer raises TransportError."""
        return await self._purchase("/emails", self.email_body(site, domain, max_price), idempotency_key, "email")

    async def email(self, nh_id: int) -> dict:
        """The order with its `email` block: {address, code, link, links, message}."""
        return (await self._req("GET", f"/emails/{nh_id}"))["email"]

    async def cancel_email(self, nh_id: int) -> dict:
        return await self._req("DELETE", f"/emails/{nh_id}")

    # ── proxy data packages: residential data in GB, paid up front ────────────
    async def proxy_packages(self) -> dict:
        """{enabled, validity_days, packages: [{gb, price, per_gb, save_percent, popular}]}."""
        return await self._req("GET", "/proxy/packages")

    @staticmethod
    def proxy_body(gb: int, max_price: Decimal) -> bytes:
        return json.dumps({"gb": int(gb), "max_price": f"{max_price:.2f}"},
                          separators=(",", ":"), sort_keys=True).encode()

    async def buy_proxy(self, gb: int, max_price: Decimal, idempotency_key: str) -> dict:
        """A data package of `gb` GB. Same rules as buy(): the key makes a retry return
        the same package, and an unclear answer raises TransportError. It is paid for
        at once and cannot be given back."""
        return await self._purchase("/proxies", self.proxy_body(gb, max_price), idempotency_key, "proxy")

    async def proxy(self, nh_id: int) -> dict:
        """The package with how to connect, and what is used when that can be read:
        the order plus its `proxy` block {host, ip, http_port, socks_port, username,
        password, gb, lines, usage}."""
        try:
            return (await self._req("GET", f"/proxies/{int(nh_id)}/usage"))["proxy"]
        except TransportError:
            return (await self._req("GET", f"/proxies/{int(nh_id)}"))["proxy"]

    # ── rented numbers: a number for days, every SMS, paid up front ───────────
    async def rent_durations(self) -> list[dict]:
        """The periods a number can be rented for: [{h, label}] (h = hours)."""
        rows = (await self._req("GET", "/rent/durations")).get("durations", [])
        return [r for r in rows if isinstance(r, dict) and r.get("h")] if isinstance(rows, list) else []

    async def rent_countries(self, h: int) -> list[dict]:
        """Countries with numbers to rent for `h` hours: [{country, name, flag}]."""
        rows = (await self._req("GET", f"/rent/{int(h)}/countries")).get("countries", [])
        return [r for r in rows if isinstance(r, dict) and r.get("country") is not None] if isinstance(rows, list) else []

    async def rent_services(self, h: int, country: str) -> list[dict]:
        """What a number of this country can be rented for: [{code, name, price, in_stock, popular}]."""
        rows = (await self._req("GET", f"/rent/{int(h)}/{quote(str(country), safe='')}/services")).get("services", [])
        return [r for r in rows if isinstance(r, dict) and r.get("code")] if isinstance(rows, list) else []

    @staticmethod
    def rent_body(h: int, country: str, code: str, max_price: Decimal) -> bytes:
        return json.dumps({"h": int(h), "country": str(country), "code": str(code), "max_price": f"{max_price:.2f}"},
                          separators=(",", ":"), sort_keys=True).encode()

    async def buy_rental(self, h: int, country: str, code: str, max_price: Decimal, idempotency_key: str) -> dict:
        """Rent a number for `h` hours. Same rules as buy(): the key makes a retry return
        the same rental, and an unclear answer raises TransportError. It is paid for at
        once; NumberHub gives the money back only for a cancel inside its refund window."""
        return await self._purchase("/rentals", self.rent_body(h, country, code, max_price), idempotency_key, "rental")

    async def rental(self, nh_id: int) -> dict:
        """The rental with `messages` (every SMS: {from, text, code}) and, while a cancel
        can still be refunded, `refund_from` / `refund_until`."""
        return (await self._req("GET", f"/rentals/{int(nh_id)}"))["rental"]

    async def cancel_rental(self, nh_id: int) -> dict:
        """{ok, rental}: ok false = not cancelled, nothing refunded. Outside the refund
        window: 409 cancel_window_closed with window_state "locked" (too early) or "closed"."""
        return await self._req("DELETE", f"/rentals/{int(nh_id)}")

    # ── deposits into the reseller's wallet (built-in payments) ───────────────
    async def open_topup(self, amount: Decimal, method: str, reference: str, lifetime_minutes: int) -> dict:
        """Open a USDT deposit for ONE of the shop's customers (`reference` says
        which). A lost reply is asked again: the same amount, network and reference
        return the deposit that was opened, never a second one."""
        body = json.dumps({"amount": f"{amount:.2f}", "method": method, "reference": reference,
                           "lifetime_minutes": int(lifetime_minutes)},
                          separators=(",", ":"), sort_keys=True).encode()
        last: TransportError | None = None
        for attempt in range(3):
            try:
                r = await self._send("POST", "/topups", content=body)
                data = r.json()
                return data["topup"] if isinstance(data, dict) and isinstance(data.get("topup"), dict) else {}
            except TransportError as exc:
                last = exc
                await asyncio.sleep(1.5 * (attempt + 1))
        assert last is not None
        raise last

    async def topup_methods(self) -> list[dict]:
        """Every coin and network a deposit can be opened in: [{id, coin, network,
        network_short, partial_credit}]."""
        data = await self._req("GET", "/topups/methods")
        rows = data.get("methods")
        return [m for m in rows if isinstance(m, dict) and m.get("id") and m.get("coin")] if isinstance(rows, list) else []

    async def topup(self, topup_id: int) -> dict:
        data = await self._req("GET", f"/topups/{int(topup_id)}")
        return data["topup"] if isinstance(data.get("topup"), dict) else {}
