"""What one US dollar costs in Indonesian rupiah today.

For shops that take QRIS payments (app/payments.py, KaseraPay) and let the rate
follow the market instead of typing one. Read from public sources that need no
key and are sent nothing about anyone; the last good value is kept in the
database, so a restart, or a source that is down, changes nothing for days.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal

import httpx

from app.db import session_factory
from app.models import Setting

log = logging.getLogger(__name__)

# (address, where the number is in the answer). Asked in this order; the first
# sensible answer is used.
SOURCES = (
    ("https://open.er-api.com/v6/latest/USD", ("rates", "IDR")),
    ("https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.min.json", ("usd", "idr")),
)
SANE = (Decimal("10000"), Decimal("30000"))     # anything else is a broken answer, not a rate
REFRESH_SEC = 3 * 3600
MAX_AGE_SEC = 4 * 86400                         # older than this, a kept rate is no longer used
KEY = "usd_idr"
TRANSPORT: httpx.AsyncBaseTransport | None = None       # tests put a fake here
_rate: tuple[float, Decimal] | None = None      # (unix time it was read, rupiah for one dollar)


def _sane(value) -> Decimal | None:
    try:
        rate = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None
    return rate if rate.is_finite() and SANE[0] <= rate <= SANE[1] else None


def current() -> Decimal | None:
    """Rupiah for one dollar, or None while no rate is known (or the one kept is too old)."""
    if _rate is None or time.time() - _rate[0] > MAX_AGE_SEC:
        return None
    return _rate[1]


def age_hours() -> float | None:
    return None if _rate is None else max(0.0, (time.time() - _rate[0]) / 3600)


async def _fetch() -> Decimal | None:
    async with httpx.AsyncClient(timeout=12.0, transport=TRANSPORT, follow_redirects=True) as c:
        for url, path in SOURCES:
            try:
                r = await c.get(url)
                value = r.json() if r.status_code == 200 else None
                for part in path:
                    value = value.get(part) if isinstance(value, dict) else None
            except (httpx.HTTPError, ValueError):
                continue
            rate = _sane(value)
            if rate is not None:
                return rate
            log.warning("exchange rate: %s gave no usable rupiah rate", url.split("/")[2])
    return None


async def load() -> Decimal | None:
    """At start: the rate kept from before, so nothing waits for the first reading."""
    global _rate
    async with session_factory() as s:
        row = await s.get(Setting, KEY)
    try:
        value, when = str(row.value).split("|", 1) if row is not None else ("", "0")
        rate = _sane(value)
        if rate is not None:
            _rate = (float(when), rate)
    except (TypeError, ValueError):
        pass
    return current()


async def refresh() -> Decimal | None:
    """Read today's rate and keep it. A reading that fails changes nothing."""
    global _rate
    rate = await _fetch()
    if rate is None:
        log.warning("exchange rate: no source answered; using the one kept (%s)", current())
        return current()
    _rate = (time.time(), rate)
    async with session_factory() as s:
        row = await s.get(Setting, KEY)
        if row is None:
            s.add(Setting(key=KEY, value=f"{rate}|{_rate[0]}"))
        else:
            row.value = f"{rate}|{_rate[0]}"
        await s.commit()
    return rate
