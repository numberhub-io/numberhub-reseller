"""NumberHub public API client (https://api.numberhub.io/v1, Bearer API key).

The reseller's own API key is used, so every number is bought on — and paid
from — the reseller's NumberHub wallet at the price NumberHub gives them.
Money comes back as 2-dp strings; errors as {"error": code, ...}.
"""
from __future__ import annotations

import asyncio
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

    async def _send(self, method: str, path: str, *, params=None, json=None, headers=None) -> httpx.Response:
        hdrs = {"Authorization": f"Bearer {self._key}", "Accept": "application/json", **(headers or {})}
        try:
            r = await self._http.request(method, self._base + path, params=params, json=json, headers=hdrs)
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
    async def buy(self, service: str, country: str, max_price: Decimal, idempotency_key: str) -> dict:
        """Buy a number. The Idempotency-Key makes a retry after a lost reply
        return the same order instead of buying a second one."""
        body = {"service": service, "country": str(country), "max_price": f"{max_price:.2f}"}
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = await self._send("POST", "/numbers", json=body,
                                     headers={"Idempotency-Key": idempotency_key})
                return r.json()["number"]
            except TransportError as exc:
                last = exc
                await asyncio.sleep(1.5 * (attempt + 1))
            except NumberHubError as exc:
                # A retry racing the original still in flight: wait for it to land.
                if exc.code == "idempotency_in_progress" and attempt < 2:
                    last = exc
                    await asyncio.sleep(2)
                    continue
                raise
        raise last  # type: ignore[misc]

    async def number(self, nh_id: int) -> dict:
        return (await self._req("GET", f"/numbers/{nh_id}"))["number"]

    async def cancel(self, nh_id: int) -> dict:
        return await self._req("DELETE", f"/numbers/{nh_id}")

    async def orders(self, limit: int = 100) -> list[dict]:
        return (await self._req("GET", "/orders", params={"limit": limit})).get("orders", [])
