"""NumberHub public API client (https://api.numberhub.io/v1, Bearer API key).

The reseller's own API key is used, so every number is bought on — and paid
from — the reseller's NumberHub wallet at the price NumberHub gives them.
Money comes back as 2-dp strings; errors as {"error": code, ...}.
"""
from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal

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
        body = self.buy_body(service, country, max_price)
        last: NumberHubError | None = None
        maybe_sent = False
        for attempt in range(3):
            try:
                r = await self._send("POST", "/numbers", content=body,
                                     headers={"Idempotency-Key": idempotency_key})
                return r.json()["number"]
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

    async def orders(self, limit: int = 100) -> list[dict]:
        return (await self._req("GET", "/orders", params={"limit": limit})).get("orders", [])
