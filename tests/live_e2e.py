"""LIVE end-to-end check against the real NumberHub API with a real key, on a
throw-away database (your running bots are not touched). It buys the cheapest
in-stock WhatsApp number, checks the hold on both sides, checks the cancel lock,
waits for it, cancels, checks both refunds, and checks the Idempotency-Key
replay returns the SAME order. No code is requested, so nothing is charged
(unless an SMS happens to land on the number during the ~2 minutes).
    NH_KEY=nh_live_... python tests/live_e2e.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
KEY = os.environ.get("NH_KEY", "")
_tmp = tempfile.TemporaryDirectory()
os.environ["DB_URL"] = "sqlite+aiosqlite:///" + (Path(_tmp.name) / "live.db").as_posix()
from cryptography.fernet import Fernet  # noqa: E402

os.environ["SECRET_KEY"] = Fernet.generate_key().decode()
os.chdir(_tmp.name)

from app import crypto, repo, selling  # noqa: E402
from app.db import init_db  # noqa: E402
from app.models import Reseller  # noqa: E402
from app.numberhub import NumberHub, NumberHubError, dec  # noqa: E402

P = F = 0


def check(label, ok, extra=""):
    global P, F
    P, F = (P + 1, F) if ok else (P, F + 1)
    print(f"  {'ok  ' if ok else 'FAIL'} {label} {extra}", flush=True)


async def main():
    if not KEY:
        raise SystemExit("set NH_KEY")
    await init_db()
    cli = NumberHub(KEY)
    r = await repo.create_reseller(owner_id=1, bot_token_enc=crypto.encrypt("1:x"), bot_id=1, bot_username="e2e",
                                   api_key_enc=crypto.encrypt(KEY), markup_pct=Decimal("30"), status=Reseller.ACTIVE)
    selling.set_client(r.id, cli)
    m = await repo.get_or_create_member(r.id, 1, "e2e", "E2E", "en")
    await repo.member_adjust(r.id, m.id, Decimal("1.00"))

    print("catalog")
    svcs = await selling.services(r)
    check("services load from the live API", len(svcs) > 100, f"({len(svcs)})")
    rows = await selling.countries(r, "wa", fresh=True)
    check("WhatsApp countries load", len(rows) > 20, f"({len(rows)})")
    ok_price = all(row["member_price"] == selling.member_price(row["ceiling"], Decimal("30")) for row in rows)
    check("every customer price = ceiling + 30%, rounded up", ok_price)
    bal0 = await cli.balance()
    avail0 = dec(bal0["available"])
    stocked = [x for x in rows if x.get("in_stock") and not x.get("rate_dead") and x["ceiling"] <= avail0]
    stocked.sort(key=lambda x: x["ceiling"])
    if not stocked:
        print("no affordable in-stock route — stopping before buying")
        return
    row = stocked[0]
    print(f"buying: WhatsApp · {row['name']} — NumberHub {row['ceiling']}, customer {row['member_price']}; "
          f"wallet {avail0}")

    print("purchase")
    try:
        order = await selling.buy(r, await repo.get_member(m.id), "wa", str(row["country"]), row["member_price"])
    except selling.SellError as exc:
        if exc.reason != "price_changed":
            raise
        # What a member sees: "price changed", the screen redraws, they tap again.
        row = next(x for x in await selling.countries(r, "wa") if str(x["country"]) == str(row["country"]))
        check("refused once -> the screen redraws with NumberHub's real price",
              row["member_price"] == exc.price, f"customer {row['member_price']} (NumberHub {row['ceiling']})")
        if row["ceiling"] > avail0:
            print("the real price is above the wallet — stopping before buying")
            return
        order = await selling.buy(r, await repo.get_member(m.id), "wa", str(row["country"]), row["member_price"])
    mm = await repo.get_member(m.id)
    check("customer price held on the member, not charged", mm.held == row["member_price"] and mm.balance == Decimal("1.00"))
    bal1 = await cli.balance()
    check("NumberHub wallet: the ceiling is held, nothing charged",
          dec(bal1["held"]) - dec(bal0["held"]) == row["ceiling"] and dec(bal1["balance"]) == dec(bal0["balance"]),
          f"held {bal0['held']} -> {bal1['held']}")
    check("a real number was issued", order.status in ("waiting", "pending") and order.nh_id, f"#{order.nh_id} {order.phone}")

    print("sync")
    await selling.sync_reseller(r)
    o = await repo.get_order(order.id)
    check("sync mirrors NumberHub's order (status, number, countdown)",
          o.status == "waiting" and o.expires_at is not None and o.cancel_available_at is not None, o.status)

    print("replay")
    again = await cli.buy("wa", str(row["country"]), row["ceiling"], selling.idempotency_key(order))
    check("the same Idempotency-Key replays the SAME order (no second purchase)", int(again["id"]) == o.nh_id)
    bal2 = await cli.balance()
    check("…and nothing more is held", dec(bal2["held"]) == dec(bal1["held"]))

    print("cancel")
    ok, why, secs = await selling.cancel(r, await repo.get_member(m.id), order.id)
    check("cancel is locked at first (supplier rule)", not ok and why == "locked" and secs > 0, f"{secs}s")
    wait = max(0, int((o.cancel_available_at.replace(tzinfo=dt.timezone.utc) if o.cancel_available_at.tzinfo is None
                       else o.cancel_available_at).timestamp() - dt.datetime.now(dt.timezone.utc).timestamp())) + 8
    print(f"  waiting {wait}s for the cancel window…", flush=True)
    await asyncio.sleep(wait)
    for _ in range(6):
        ok, why, secs = await selling.cancel(r, await repo.get_member(m.id), order.id)
        if ok or why not in ("locked", "busy"):
            break
        await asyncio.sleep(max(5, secs or 5))
    o = await repo.get_order(order.id)
    mm = await repo.get_member(m.id)
    if why == "code_received":
        print("  a code arrived before the cancel — the order is delivered and charged instead")
        check("code arrived: member charged exactly once", mm.balance == Decimal("1.00") - row["member_price"] and mm.held == 0)
    else:
        check("cancelled", ok and o.status == "canceled", f"{why} {o.status}")
        check("customer: hold released, nothing charged", mm.held == 0 and mm.balance == Decimal("1.00"))
        await asyncio.sleep(2)
        bal3 = await cli.balance()
        check("NumberHub wallet: hold released, nothing charged",
              dec(bal3["held"]) == dec(bal0["held"]) and dec(bal3["balance"]) == dec(bal0["balance"]),
              f"balance {bal0['balance']} -> {bal3['balance']}, held {bal3['held']}")
        nh = await cli.number(o.nh_id)
        check("NumberHub shows the order cancelled", nh["status"] == "canceled", nh["status"])
    await cli.close()
    print(f"\nRESULT: {P} passed, {F} failed")


try:
    asyncio.run(main())
except NumberHubError as exc:
    print("API error:", exc.code, exc.status, exc.data)
sys.stdout.flush()
os._exit(0 if F == 0 else 1)
