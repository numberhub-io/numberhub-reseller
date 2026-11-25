"""End-to-end tests: the real selling code, repo, bots and handlers against an
in-memory NumberHub API and a fake Telegram session.
    python tests/run_tests.py          (prints RESULT: N passed, M failed)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import itertools
import os
import re
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
_tmp = tempfile.TemporaryDirectory()
os.environ["DB_URL"] = "sqlite+aiosqlite:///" + (Path(_tmp.name) / "t.db").as_posix()
from cryptography.fernet import Fernet  # noqa: E402

os.environ["SECRET_KEY"] = Fernet.generate_key().decode()
os.environ["ADMIN_IDS"] = "1"
os.chdir(_tmp.name)  # no stray .env from the developer's checkout

from aiogram import Dispatcher  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from aiogram.types import Update  # noqa: E402

from app import crypto, repo, runtime, selling  # noqa: E402
from app.db import init_db  # noqa: E402
from app.i18n import LANGS, _table, t  # noqa: E402
from app.models import Order, Reseller  # noqa: E402
from fakes import iso as fakes_iso  # noqa: E402
from app.selling import SellError, member_price, original_ceiling  # noqa: E402
from fakes import FakeNumberHub, FakeSession, fake_bot  # noqa: E402
from app.bots.callbacks import Adm, Ord  # noqa: E402

P = F = 0
D = Decimal
OWNER, ALICE, BOB = 5001, 6001, 6002


def check(label, ok, extra=""):
    global P, F
    if ok:
        P += 1
        print(f"  ok   {label} {extra}")
    else:
        F += 1
        print(f"  FAIL {label} {extra}")


async def make_reseller(api: FakeNumberHub, markup="30", owner=OWNER, bot_id=777):
    r = await repo.create_reseller(owner_id=owner, bot_token_enc=crypto.encrypt("123456:" + "A" * 35), bot_id=bot_id,
                                   bot_username="my_shop_bot", bot_title="My Shop",
                                   api_key_enc=crypto.encrypt(api.key), markup_pct=D(markup),
                                   status=Reseller.ACTIVE)
    selling.set_client(r.id, api.client())
    return r


async def member(r, tg, credit="0"):
    m = await repo.get_or_create_member(r.id, tg, f"user{tg}", f"User {tg}", "en")
    if D(credit) > 0:
        await repo.member_adjust(r.id, m.id, D(credit))
    return await repo.get_member(m.id)


async def forget_paid_in():
    """One owner's shops share one wallet, so paid-in money a test leaves behind would
    count against the wallet of the next test's shop. Each test is its own world."""
    from sqlalchemy import text as sql_text
    from app import db as app_db
    async with app_db.engine.begin() as conn:
        await conn.execute(sql_text("UPDATE members SET paid_left = 0"))


# ─── pure ────────────────────────────────────────────────────────────────────
def test_prices():
    print("prices")
    check("30% on $0.30 = $0.39", member_price(D("0.30"), D("30")) == D("0.39"))
    check("always rounds UP to the cent", member_price(D("0.33"), D("10")) == D("0.37"))
    ok = all(original_ceiling(member_price(D(c) / 100, D(mk)), D(mk)) == D(c) / 100
             for c in range(1, 600, 7) for mk in ("0", "5", "12.5", "30", "99", "300"))
    check("the purchase body is rebuilt exactly from the member price (replay safety)", ok)
    check("crypto round trip", crypto.decrypt(crypto.encrypt("secret-1")) == "secret-1")


def test_i18n():
    print("translations")
    en = _table("en")
    ph = lambda s: sorted(set(re.findall(r"\{(\w+)\}", s)))  # noqa: E731
    tags = lambda s: sorted(re.findall(r"</?(?:b|i|code)>", s))  # noqa: E731
    for code in LANGS:
        tab = _table(code)
        missing = [k for k in en if k not in tab]
        bad_ph = [k for k in en if k in tab and ph(tab[k]) != ph(en[k])]
        bad_tags = [k for k in en if k in tab and tags(tab[k]) != tags(en[k])]
        check(f"{code}: all {len(en)} strings, same placeholders and tags",
              not missing and not bad_ph and not bad_tags,
              f"missing={missing[:4]} placeholders={bad_ph[:4]} tags={bad_tags[:4]}")
    check("unknown language falls back to English", t("xx", "btn_buy") == en["btn_buy"])


# ─── money flows ─────────────────────────────────────────────────────────────
async def test_buy_and_code():
    print("buy -> code -> charged once")
    api = FakeNumberHub()
    r = await make_reseller(api)
    m = await member(r, ALICE, "5")
    o = await selling.buy(r, m, "wa", "187", D("0.39"))
    mm = await repo.get_member(m.id)
    check("member price held, not charged", mm.balance == D("5") and mm.held == D("0.39"), f"{mm.balance}/{mm.held}")
    post = [c for c in api.calls if c[0] == "POST"]
    check("one NumberHub order, on the reseller's key", len(post) == 1 and len(api.orders) == 1)
    nh = api.orders[o.nh_id]
    check("bought at NumberHub's ceiling, reseller wallet held", nh["price"] == "0.30" and api.held == D("0.30"))
    check("local order is waiting with the number", o.status == "waiting" and o.phone and o.nh_id)
    session = FakeSession()
    runtime._bots[r.id] = fake_bot(session)
    api.set_status(o.nh_id, "received", "123456")
    await selling.sync_reseller(r)
    mm = await repo.get_member(m.id)
    check("code arrived -> member charged exactly the price", mm.balance == D("4.61") and mm.held == 0,
          f"{mm.balance}/{mm.held}")
    check("member told the code", any("123456" in x for x in session.texts()))
    n_msgs = len(session.texts())
    await selling.sync_reseller(r)
    await selling.settle_sweep()
    mm = await repo.get_member(m.id)
    check("a second sync neither re-charges nor re-announces",
          mm.balance == D("4.61") and len([x for x in session.texts() if "123456" in x and "🔑" in x]) == 1,
          f"msgs {n_msgs}->{len(session.texts())}")
    api.set_status(o.nh_id, "received", "654321")
    await selling.sync_reseller(r)
    check("a second code on the same number is announced (no extra charge)",
          any("654321" in x for x in session.texts()) and (await repo.get_member(m.id)).balance == D("4.61"))
    runtime._bots.pop(r.id, None)


async def test_no_code_refund():
    print("no code -> refunded")
    api = FakeNumberHub()
    r = await make_reseller(api, bot_id=778)
    m = await member(r, ALICE, "1")
    o = await selling.buy(r, m, "wa", "6", None)
    check("Indonesia priced from its ceiling ($0.25 + 30%)", o.member_price == D("0.33"), str(o.member_price))
    session = FakeSession()
    runtime._bots[r.id] = fake_bot(session)
    api.set_status(o.nh_id, "expired")
    await selling.sync_reseller(r)
    mm = await repo.get_member(m.id)
    check("expired without a code -> nothing charged", mm.balance == D("1") and mm.held == 0)
    check("member told about the refund", any("back on your balance" in x for x in session.texts()))
    fresh = await repo.get_order(o.id)
    check("order settled as released", fresh.settled == Order.RELEASED)
    runtime._bots.pop(r.id, None)


async def test_failures():
    print("purchase failures give the hold back")
    api = FakeNumberHub()
    r = await make_reseller(api, bot_id=779)
    m = await member(r, ALICE, "0.20")
    try:
        await selling.buy(r, m, "wa", "187", None)
        check("not enough member credit is refused", False)
    except SellError as e:
        check("not enough member credit is refused before NumberHub is called",
              e.reason == "no_credit" and not [c for c in api.calls if c[0] == "POST"])
    await repo.member_adjust(r.id, m.id, D("5"))
    m = await repo.get_member(m.id)
    session = FakeSession()
    runtime._bots[r.id] = fake_bot(session)
    api.fail_next_buy = "insufficient_funds"
    try:
        await selling.buy(r, m, "wa", "187", None)
    except SellError as e:
        await asyncio.sleep(0.05)
        mm = await repo.get_member(m.id)
        check("reseller wallet empty -> 'paused', member hold released", e.reason == "paused" and mm.held == 0)
        check("the reseller is warned in their own bot", any("NumberHub balance is too low" in x
                                                            for x in session.texts()))
    api.fail_next_buy = "price_exceeded"
    try:
        await selling.buy(r, m, "wa", "187", None)
    except SellError as e:
        check("NumberHub price moved -> price_changed with the new member price",
              e.reason == "price_changed" and e.price == D("0.59"), str(e.price))
    try:
        await selling.buy(r, m, "wa", "187", D("0.35"))
    except SellError as e:
        check("the price on the tapped button is enforced before any hold",
              e.reason == "price_changed" and (await repo.get_member(m.id)).held == 0)
    try:
        await selling.buy(r, m, "wa", "999", None)
    except SellError as e:
        check("unknown country -> sold_out", e.reason == "sold_out")
    failed = [o for o in await repo.member_orders(m.id, 50)]
    check("failed purchases are hidden from the member's orders", not failed)
    # The list under-reports what a buy really reserves (live NumberHub did, from
    # 09-23 to 10-02): refused once, then the real price is shown and the next tap buys.
    api.true_reserve[("wa", "6")] = "0.40"                 # listed price_max 0.25
    rows = await selling.countries(r, "wa", fresh=True)
    shown = next(x for x in rows if x["country"] == "6")["member_price"]
    try:
        await selling.buy(r, await repo.get_member(m.id), "wa", "6", shown)
        check("an under-reported route is refused once", False)
    except SellError as e:
        check("an under-reported route is refused once, with the real price",
              e.reason == "price_changed" and e.price == D("0.52") and (await repo.get_member(m.id)).held == 0,
              f"{shown} -> {e.price}")
    again = next(x for x in await selling.countries(r, "wa") if x["country"] == "6")["member_price"]
    check("the screen redraws with the price NumberHub really reserves", again == D("0.52"), str(again))
    o = await selling.buy(r, await repo.get_member(m.id), "wa", "6", again)
    check("the next tap buys (no endless 'price changed' loop)",
          o.status == "waiting" and o.member_price == D("0.52"), o.status)
    runtime._bots.pop(r.id, None)


async def test_lost_reply():
    print("lost replies never buy twice")
    api = FakeNumberHub()
    r = await make_reseller(api, bot_id=780)
    m = await member(r, ALICE, "5")
    api.lose_replies = 1
    o = await selling.buy(r, m, "wa", "187", None)
    check("one lost reply: retried with the same key, ONE order", len(api.orders) == 1 and o.nh_id in api.orders)
    api.lose_replies = 3
    try:
        await selling.buy(r, m, "tg", "187", None)
        check("three lost replies -> processing", False)
    except SellError as e:
        check("three lost replies -> 'processing', hold kept", e.reason == "processing"
              and (await repo.get_member(m.id)).held == D("0.39") + D("1.43"))
    buying = [x for x in await repo.member_orders(m.id, 20) if x.status == Order.BUYING]
    check("the order waits in BUYING", len(buying) == 1)
    from app import repo as _r
    orig = _r.stale_buying
    _r.stale_buying = lambda older_than_sec=60: orig(older_than_sec=0)
    try:
        await selling.recover_buying()
    finally:
        _r.stale_buying = orig
    rec = await repo.get_order(buying[0].id)
    check("recovery replays the same request: linked, still ONE order for it",
          rec.nh_id is not None and len(api.orders) == 2 and rec.status == "waiting")
    # A restored backup, or a second database on the same API key, repeats order
    # ids. NumberHub keeps a key for 24 h, so a repeated key would be refused
    # (idempotency_conflict, seen live) or replay the OLD order and its code.
    from types import SimpleNamespace
    import datetime as _dt
    twin = SimpleNamespace(reseller_id=rec.reseller_id, id=rec.id,
                           created_at=rec.created_at + _dt.timedelta(microseconds=1))
    check("same ids in another database -> a different Idempotency-Key",
          selling.idempotency_key(twin) != selling.idempotency_key(rec), selling.idempotency_key(rec))
    check("the key is stable across reads (what recovery relies on)",
          selling.idempotency_key(await repo.get_order(rec.id)) == selling.idempotency_key(rec))


async def test_cancel():
    print("cancel")
    api = FakeNumberHub()
    r = await make_reseller(api, bot_id=781)
    m = await member(r, ALICE, "5")
    o = await selling.buy(r, m, "wa", "187", None)
    api.cancel_lock = 90
    ok, why, secs = await selling.cancel(r, m, o.id)
    check("supplier lock -> not cancelled, seconds shown, hold kept",
          not ok and why == "locked" and secs == 90 and (await repo.get_member(m.id)).held == D("0.39"))
    api.cancel_lock = 0
    ok, why, _ = await selling.cancel(r, m, o.id)
    mm = await repo.get_member(m.id)
    check("cancel -> released, nothing charged", ok and mm.held == 0 and mm.balance == D("5"))
    o2 = await selling.buy(r, m, "wa", "187", None)
    api.cancel_code_first = True
    ok, why, _ = await selling.cancel(r, m, o2.id)
    mm = await repo.get_member(m.id)
    check("code beat the cancel -> delivered and charged", not ok and why == "code_received"
          and mm.balance == D("4.61") and mm.held == 0, f"{mm.balance}/{mm.held}")
    bob = await member(r, BOB, "1")
    ok, why, _ = await selling.cancel(r, bob, o2.id)
    check("a member cannot touch someone else's order", not ok and why == "not_found")


async def test_races_and_limits():
    print("races and limits")
    api = FakeNumberHub(wallet="100")
    r = await make_reseller(api, bot_id=782)
    m = await member(r, ALICE, "1")
    results = await asyncio.gather(*[selling.buy(r, m, "wa", c, None) for c in ("187", "6", "187", "6", "187")],
                                   return_exceptions=True)
    bought = [x for x in results if isinstance(x, Order)]
    mm = await repo.get_member(m.id)
    check("5 parallel taps on $1 credit never overspend", mm.held <= D("1") and mm.held == sum(o.member_price for o in bought),
          f"bought {len(bought)} held {mm.held}")
    check("NumberHub orders == local orders bought", len(api.orders) == len(bought))
    o = bought[0]
    api.set_status(o.nh_id, "received", "1")
    fresh = await repo.get_order(o.id)
    await repo.apply_nh_state(o.id, api.orders[o.nh_id])
    fresh = await repo.get_order(o.id)
    outs = await asyncio.gather(*[selling.settle_order(fresh) for _ in range(5)])
    check("five concurrent settles charge once", sum(1 for x in outs if x) == 1)
    big = await member(r, BOB, "100")
    for _ in range(3):
        await selling.buy(r, big, "tg", "187", None)
    try:
        await selling.buy(r, big, "tg", "187", None)
        check("route limit", False)
    except SellError as e:
        check(f"at most {3} open orders per route per member", e.reason == "too_many_open")
    ok = await repo.member_adjust(r.id, big.id, D("-99"))
    check("the reseller can't remove credit reserved by open orders", not ok)
    await repo.set_member_blocked(m.id, True)
    try:
        await selling.buy(r, await repo.get_member(m.id), "wa", "187", None)
    except SellError as e:
        check("a blocked member can't buy", e.reason == "blocked")


# ─── the bot itself (handlers through aiogram) ───────────────────────────────
_uid = itertools.count(1)


def _user(uid, lang="en", username="alice"):
    return {"id": uid, "is_bot": False, "first_name": "Alice", "username": username, "language_code": lang}


def msg(uid, text, lang="en"):
    ents = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else None
    return Update.model_validate({"update_id": next(_uid), "message": {
        "message_id": next(_uid), "date": 0, "chat": {"id": uid, "type": "private"}, "from": _user(uid, lang),
        "text": text, **({"entities": ents} if ents else {})}})


def tap(uid, data, mid=50, lang="en"):
    return Update.model_validate({"update_id": next(_uid), "callback_query": {
        "id": str(next(_uid)), "from": _user(uid, lang), "chat_instance": "c", "data": data,
        "message": {"message_id": mid, "date": 0, "chat": {"id": uid, "type": "private"}, "text": "x",
                    "from": {"id": 777, "is_bot": True, "first_name": "Bot"}}}})


def button(session: FakeSession, contains: str) -> str | None:
    mk = session.last_markup()
    for row in (mk.inline_keyboard if mk else []):
        for b in row:
            if contains in b.text:
                return b.callback_data
    return None


async def test_bot_flow():
    print("the bot, tap by tap")
    from app.bots.reseller import build_router
    api = FakeNumberHub()
    r = await make_reseller(api, bot_id=790)
    session = FakeSession()
    bot = fake_bot(session)
    runtime._bots[r.id] = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(r.id))
    await dp.feed_update(bot, msg(ALICE + 100, "/start"))
    check("/start shows the welcome and balance", "Welcome to" in session.last_text() and "$0.00" in session.last_text())
    check("no admin button for a customer", button(session, "Admin") is None)
    check("no separate Support button in the menu", button(session, "Support") is None)
    # A catalog shaped like the live one: most popular first, raw names, ~40 apps on S.
    selling._svc_cache = (0.0, [])
    api.services = ([{"code": "ot", "name": "Any other"}, {"code": "tg", "name": "Telegram"},
                     {"code": "wa", "name": "Whatsapp"}, {"code": "ig", "name": "Instagram+Threads"},
                     {"code": "wb", "name": "WeChat"}, {"code": "ya", "name": "yandex"},
                     {"code": "vk", "name": "vk.com"}, {"code": "cn", "name": " Caffe Nero"},
                     {"code": "gx", "name": "Google,youtube,Gmail"}]
                    + [{"code": f"s{i:02d}", "name": f"Shop {i:02d}"} for i in range(40)])
    n_apps = len(api.services)
    await dp.feed_update(bot, tap(ALICE + 100, button(session, "Buy a number")))
    check("app picker: popular apps with icons, then more popular, then A–Z",
          "Which app" in session.last_text() and button(session, "💬 WhatsApp")
          and button(session, "✈️ Telegram") and button(session, "More popular apps")
          and button(session, f"All {n_apps} apps, A–Z") and button(session, "Any other app"))
    await dp.feed_update(bot, tap(ALICE + 100, button(session, "More popular apps")))
    more = [b.text for row in session.last_markup().inline_keyboard for b in row]
    check("more popular: the curated list, in its order, for the apps the catalog has",
          "More popular apps" in session.last_text() and more[:3] == ["WeChat", "Yandex", "VK"]
          and "Caffe Nero" not in more, str(more[:5]))
    check("no wall of identical 📱 icons", not any("📱" in x for x in more))
    await dp.feed_update(bot, tap(ALICE + 100, button(session, f"All {n_apps} apps")))
    check("A–Z grid: the count, only letters that have apps",
          f"All apps ({n_apps})" in session.last_text() and button(session, "W")
          and button(session, "S") and button(session, "Q") is None)
    await dp.feed_update(bot, tap(ALICE + 100, button(session, "W")))
    w = [b.text for row in session.last_markup().inline_keyboard for b in row]
    check("a letter lists its apps A to Z, without icons",
          "W</b> · 2 apps" in session.last_text() and w[:2] == ["WeChat", "WhatsApp"], str(w[:3]))
    await dp.feed_update(bot, tap(ALICE + 100, "az:S:0"))
    s1 = [b.text for row in session.last_markup().inline_keyboard for b in row]
    check("a long letter pages at 30 with ➡️", "Shop 00" in s1 and "Shop 29" in s1
          and "Shop 30" not in s1 and "1/2" in s1 and "➡️" in s1)
    await dp.feed_update(bot, tap(ALICE + 100, button(session, "➡️")))
    s2 = [b.text for row in session.last_markup().inline_keyboard for b in row]
    check("…and the next page has the rest", "Shop 39" in s2 and "2/2" in s2 and "⬅️" in s2)
    await dp.feed_update(bot, tap(ALICE + 100, "az:C:0"))
    check("names are cleaned (a stray leading space)", button(session, "Caffe Nero") is not None)
    await dp.feed_update(bot, tap(ALICE + 100, "az:G:0"))
    check("…and commas spaced", button(session, "Google, youtube, Gmail") is not None)
    await dp.feed_update(bot, tap(ALICE + 100, "az:*:0"))
    check("an old 'All services' button opens the A–Z grid", "All apps" in session.last_text())
    await dp.feed_update(bot, tap(ALICE + 100, "az:W:0"))
    await dp.feed_update(bot, msg(ALICE + 100, "whats"))
    check("typing a name searches", "Results for" in session.last_text() and button(session, "WhatsApp"))
    await dp.feed_update(bot, tap(ALICE + 100, button(session, "WhatsApp")))
    labels = [b.text for row in session.last_markup().inline_keyboard for b in row]
    check("countries best first with the customer's price (no confusing %)",
          labels[0] == "🇺🇸 USA · $0.39" and not any("%" in x for x in labels)
          and any("⏳" in x for x in labels), labels[0])
    check("the country page says how many countries", "3 countries" in session.last_text())
    await dp.feed_update(bot, msg(ALICE + 100, "indo"))
    check("typing a country name on the country page finds it",
          "countries for" in session.last_text() and button(session, "Indonesia"))
    await dp.feed_update(bot, tap(ALICE + 100, "n:buy"))
    check("the popular grid now shows each app's cheapest price", button(session, "💬 WhatsApp · $0.33+") is not None)
    await dp.feed_update(bot, tap(ALICE + 100, "s:wa:0"))
    await dp.feed_update(bot, tap(ALICE + 100, button(session, "USA")))
    check("confirm screen: price, refund promise, balance", "$0.39" in session.last_text()
          and "automatic refund" in session.last_text() and button(session, "Add balance"))
    await dp.feed_update(bot, tap(ALICE + 100, button(session, "Buy ·")))
    check("no credit -> told how to top up with their ID", "Not enough balance" in session.last_text()
          and str(ALICE + 100) in session.last_text())
    mem = await repo.find_member(r.id, str(ALICE + 100))
    await repo.member_adjust(r.id, mem.id, D("2"))
    await dp.feed_update(bot, tap(ALICE + 100, "c:wa:187"))
    await dp.feed_update(bot, tap(ALICE + 100, button(session, "Buy ·")))
    card = session.last_text()
    check("buy -> live order card with number, countdown and reserve note",
          "+1555000" in card and "left" in card and "reserved" in card, card[:80])
    check("cancel is locked for the first minutes", button(session, "Cancel in") is not None)
    order = (await repo.member_orders(mem.id, 1))[0]
    check("the card is the message that refreshes", order.chat_id == ALICE + 100 and order.message_id)
    api.set_status(order.nh_id, "received", "778899")
    await selling.sync_reseller(r)
    check("the code reaches the member", any("778899" in x for x in session.texts()))
    await dp.feed_update(bot, tap(ALICE + 100, "n:orders"))
    check("my orders lists it with ✅", "recent orders" in session.last_text()
          and any("✅" in b.text for row in session.last_markup().inline_keyboard for b in row))
    await dp.feed_update(bot, tap(ALICE + 100, "n:again"))
    check("buy again opens the same route", "USA" in session.last_text() and "$0.39" in session.last_text())
    await dp.feed_update(bot, tap(ALICE + 100, "n:menu"))
    check("the menu offers 'Again' with the service and flag", button(session, "WhatsApp 🇺🇸") is not None)
    await dp.feed_update(bot, tap(ALICE + 100, "n:buy"))
    check("a single page shows no '1/1' row", button(session, "1/1") is None)
    n_alerts = len(session.alerts())
    await dp.feed_update(bot, tap(ALICE + 100, "bd:create:0:"))     # a builder button, stale here
    check("an unknown/old button is answered (never spins) and opens the menu",
          len(session.alerts()) == n_alerts + 1 and "old message" in session.alerts()[-1]
          and button(session, "Buy a number") is not None)
    await dp.feed_update(bot, tap(ALICE + 100, "n:lang"))
    await dp.feed_update(bot, tap(ALICE + 100, "l:ru"))
    check("language switch translates the menu", "Купить" in str(session.last_markup()))
    await dp.feed_update(bot, msg(ALICE + 200, "/start", lang="es"))
    check("a Spanish phone gets Spanish automatically", "Comprar" in str(session.last_markup()))

    print("the owner's admin panel")
    await dp.feed_update(bot, msg(OWNER, "/start"))
    check("the owner sees ⚙️ Admin panel", button(session, "Admin panel") is not None)
    await dp.feed_update(bot, msg(OWNER, "/admin"))
    dash = session.last_text()
    check("dashboard: customers, sales, commission with an example, NumberHub wallet",
          "Customers:" in dash and "commission" in dash.lower() and "you earn" in dash and "available" in dash, dash[:60])
    await dp.feed_update(bot, tap(OWNER, button(session, "Add balance")))
    await dp.feed_update(bot, msg(OWNER, f"{ALICE + 200} 3.5"))
    bob = await repo.find_member(r.id, str(ALICE + 200))
    check("add balance by ID", bob.balance == D("3.50") and "Added" in session.texts()[-1] + session.texts()[-2])
    check("the customer is told, in their language", any("3.50" in x and "saldo" in x for x in session.texts()))
    await dp.feed_update(bot, tap(OWNER, Adm(a="remove").pack()))
    await dp.feed_update(bot, msg(OWNER, f"@{bob.username} 10"))
    check("a username several customers have used is refused (send the ID)",
          "More than one" in session.last_text() and (await repo.get_member(bob.id)).balance == D("3.50"))
    await dp.feed_update(bot, msg(OWNER, f"{bob.telegram_id} 10"))
    check("can't remove more than they have", "available" in session.last_text())
    await dp.feed_update(bot, msg(OWNER, f"{bob.telegram_id} 0.004"))
    check("an amount that rounds to $0.00 is refused", "positive number" in session.last_text())
    await dp.feed_update(bot, tap(OWNER, Adm(a="markup", arg="sms").pack()))
    await dp.feed_update(bot, msg(OWNER, "50"))
    rr = await repo.get_reseller(r.id)
    check("markup change applies to prices at once", rr.markup_pct == D("50"))
    await dp.feed_update(bot, tap(ALICE + 100, "c:wa:187"))
    check("…the customer sees $0.45 now", "$0.45" in session.last_text())
    await dp.feed_update(bot, tap(OWNER, Adm(a="support").pack()))
    await dp.feed_update(bot, msg(OWNER, "@myshop_help"))
    await dp.feed_update(bot, tap(ALICE + 100, "n:balance"))
    check("support contact shown with a button", "@myshop_help" in session.last_text() and
          any(b.url == "https://t.me/myshop_help" for row in session.last_markup().inline_keyboard for b in row))
    await dp.feed_update(bot, tap(OWNER, Adm(a="welcome").pack()))
    await dp.feed_update(bot, msg(OWNER, "Best numbers in town <3"))
    await dp.feed_update(bot, msg(ALICE + 100, "/start"))
    check("custom welcome text (escaped)", "Best numbers in town &lt;3" in session.last_text())
    await dp.feed_update(bot, tap(OWNER, Adm(a="block").pack()))
    await dp.feed_update(bot, msg(OWNER, str(ALICE + 200)))
    await dp.feed_update(bot, msg(ALICE + 200, "/start"))
    check("a blocked customer is stopped", "blocked" in session.last_text().lower()
          or "bloquead" in session.last_text().lower())
    await dp.feed_update(bot, msg(ALICE + 100, "/admin"))
    check("customers can't open the admin panel", "Admin panel" not in session.last_text())
    await dp.feed_update(bot, tap(OWNER, Adm(a="broadcast").pack()))
    before = len(session.texts())
    await dp.feed_update(bot, msg(OWNER, "Sale today!"))
    await dp.feed_update(bot, tap(OWNER, button(session, "Send to")))      # shown to the owner first
    await asyncio.sleep(0.5)
    check("broadcast reaches unblocked customers", sum(1 for x in session.texts()[before:] if x == "Sale today!") >= 2)
    runtime._bots.pop(r.id, None)


async def test_builder():
    print("builder bot")
    from app.bots import builder
    from app import runtime as rt
    session = FakeSession(bot_id=4242, username="new_shop_bot")
    bot = fake_bot(session)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(builder.router)
    api = FakeNumberHub(key="nh_live_builderkey-0123456789abc")
    started = []
    orig_make, orig_start, orig_nh = rt.make_bot, rt.start, builder.NumberHub
    rt.make_bot = lambda token: fake_bot(FakeSession(bot_id=4242, username="new_shop_bot"))

    async def fake_start(res):
        started.append(res.id)
    rt.start = fake_start
    builder.NumberHub = lambda key: api.client(key)    # the fake checks the key it is sent
    try:
        await dp.feed_update(bot, msg(9001, "/start"))
        check("welcome explains the 3 steps", "Step" not in session.last_text() and "How it works" in session.last_text())
        await dp.feed_update(bot, tap(9001, "bd:create:0:"))
        await dp.feed_update(bot, msg(9001, "not a token"))
        check("a bad token is refused", "doesn't look like a bot token" in session.last_text())
        await dp.feed_update(bot, msg(9001, "123456789:" + "B" * 35))
        check("token accepted, the message is deleted", "Step 2 of 3" in session.last_text()
              and any(type(x).__name__ == "DeleteMessage" for x in session.requests))
        await dp.feed_update(bot, msg(9001, "nh_live_wrongkey-0123456789abc"))
        check("a rejected API key is refused", "rejected this key" in session.last_text())
        await dp.feed_update(bot, msg(9001, api.key))
        check("key accepted, wallet shown", "Step 3 of 3" in session.last_text() and "$50.00" in session.last_text())
        await dp.feed_update(bot, tap(9001, "bd:markup:0:30"))
        rows = await repo.list_resellers(owner_id=9001)
        check("bot created, started, secrets encrypted", len(rows) == 1 and started == [rows[0].id]
              and rows[0].api_key_enc != api.key and crypto.decrypt(rows[0].api_key_enc) == api.key
              and "is live" in session.last_text())
        await dp.feed_update(bot, tap(9001, "bd:mine:0:"))
        check("my bots shows it with pause and new key", "@new_shop_bot" in session.last_text()
              and button(session, "Pause") and button(session, "New API key"))
        await dp.feed_update(bot, tap(9002, f"bd:pause:{rows[0].id}:"))
        check("someone else can't pause it", (await repo.get_reseller(rows[0].id)).status == Reseller.ACTIVE)
        stopped = []
        orig_stop = rt.stop

        async def fake_stop(rid):
            stopped.append(rid)
        rt.stop = fake_stop
        try:
            await dp.feed_update(bot, tap(9001, f"bd:pause:{rows[0].id}:"))
        finally:
            rt.stop = orig_stop
        check("pause marks it paused but keeps the bot running (open orders finish)",
              (await repo.get_reseller(rows[0].id)).status == Reseller.DISABLED and not stopped)
    finally:
        rt.make_bot, rt.start, builder.NumberHub = orig_make, orig_start, orig_nh


async def test_provision():
    """Hosted shops: NumberHub's bot creates a shop from a token + a key it minted."""
    print("hosted shops (provisioning)")
    from aiohttp.test_utils import TestClient, TestServer
    from app import provision
    from app import runtime as rt
    from app.config import settings
    api = FakeNumberHub(key="nh_live_hostedkey-0123456789abcd")
    started, restarted = [], []
    orig = (rt.make_bot, rt.start, rt.restart, provision.NumberHub, settings.provision_secret)
    sessions = {}

    def make(token):
        bot_id = 7700 + int(token.split(":")[0][-2:])
        sessions[token] = FakeSession(bot_id=bot_id, username=f"hosted{bot_id}_bot")
        if token.endswith("REVOKED"):
            sessions[token].fail_get_me = 99
        return fake_bot(sessions[token])

    async def fake_start(res):
        started.append(res.id)

    async def fake_restart(rid):
        restarted.append(rid)
    rt.make_bot, rt.start, rt.restart = make, fake_start, fake_restart
    provision.NumberHub = lambda key: api.client(key)
    settings.provision_secret = "s" * 40
    tok = lambda n, tail="": f"1234567{n:02d}:" + ("C" * 35 + tail)[-35:]  # noqa: E731
    client = TestClient(TestServer(provision.build_app()))
    await client.start_server()
    H = {"X-Provision-Secret": "s" * 40}
    try:
        r = await client.post("/internal/shops", json={"owner_id": 9101, "token": tok(1), "api_key": api.key})
        check("no secret: refused before anything", r.status == 403)
        r = await client.post("/internal/shops", headers={"X-Provision-Secret": "s" * 39 + "x"},
                              json={"owner_id": 9101, "token": tok(1), "api_key": api.key})
        check("wrong secret: refused", r.status == 403)
        r = await client.post("/internal/shops", headers=H, json={"owner_id": 9101, "token": "nope", "api_key": api.key})
        check("not a token: bad_token", r.status == 400 and (await r.json())["error"] == "bad_token")
        r = await client.post("/internal/shops", headers=H,
                              json={"owner_id": 9101, "token": tok(1, "REVOKED"), "api_key": api.key})
        check("a token Telegram refuses: token_rejected", r.status == 400 and (await r.json())["error"] == "token_rejected")
        r = await client.post("/internal/shops", headers=H,
                              json={"owner_id": 9101, "token": tok(1), "api_key": "nh_live_wrongkey-0123456789abc"})
        check("a key NumberHub refuses: key_rejected, no shop",
              r.status == 409 and (await r.json())["error"] == "key_rejected"
              and not await repo.list_resellers(owner_id=9101))
        r = await client.post("/internal/shops", headers=H,
                              json={"owner_id": 9101, "owner_username": "shopowner", "token": tok(1),
                                    "api_key": api.key, "markup_pct": "25"})
        body = await r.json()
        rows = await repo.list_resellers(owner_id=9101)
        check("token + key: the shop is created and started, secrets encrypted",
              r.status == 201 and len(rows) == 1 and started == [rows[0].id]
              and crypto.decrypt(rows[0].api_key_enc) == api.key and crypto.decrypt(rows[0].bot_token_enc) == tok(1)
              and rows[0].markup_pct == D("25") and rows[0].support_contact == "@shopowner"
              and body["shop"]["bot_username"] == "hosted7701_bot" and body["wallet"] == "50.00", str(body))
        r = await client.post("/internal/shops", headers=H,
                              json={"owner_id": 9101, "token": tok(1), "api_key": api.key})
        check("the same bot again: reconnected in place (customers and balances stay)",
              r.status == 200 and (await r.json())["reconnected"] and restarted == [rows[0].id]
              and len(await repo.list_resellers(owner_id=9101)) == 1)
        r = await client.post("/internal/shops", headers=H,
                              json={"owner_id": 9102, "token": tok(1), "api_key": api.key})
        check("someone else's bot: taken", r.status == 409 and (await r.json())["error"] == "taken")
        r = await client.post("/internal/shops", headers=H,
                              json={"owner_id": 9101, "token": tok(2), "api_key": api.key, "markup_pct": "999"})
        check("a commission over the maximum is refused", r.status == 400)
        for n in (2, 3):
            await client.post("/internal/shops", headers=H, json={"owner_id": 9101, "token": tok(n), "api_key": api.key})
        r = await client.post("/internal/shops", headers=H, json={"owner_id": 9101, "token": tok(4), "api_key": api.key})
        check("at most 3 shops per owner", r.status == 409 and (await r.json())["error"] == "too_many"
              and len(await repo.list_resellers(owner_id=9101)) == 3)
        r = await client.get("/internal/shops?owner_id=9101", headers=H)
        shops = (await r.json())["shops"]
        check("the owner's shops, with this week's numbers", r.status == 200 and len(shops) == 3
              and {"members", "orders_7d", "profit_7d", "status"} <= set(shops[0]))
        rid = rows[0].id
        r = await client.post(f"/internal/shops/{rid}/status", headers=H, json={"owner_id": 9102, "status": "disabled"})
        check("someone else can't pause it", r.status == 404)
        r = await client.post(f"/internal/shops/{rid}/status", headers=H, json={"owner_id": 9101, "status": "disabled"})
        check("pause: the shop shows paused", r.status == 200
              and (await repo.get_reseller(rid)).status == Reseller.DISABLED)
        r = await client.post(f"/internal/shops/{rid}/status", headers=H, json={"owner_id": 9101, "status": "active"})
        check("resume: selling again", r.status == 200 and (await repo.get_reseller(rid)).status == Reseller.ACTIVE)
        await repo.update_reseller(rid, status=Reseller.SUSPENDED)
        r = await client.post(f"/internal/shops/{rid}/status", headers=H, json={"owner_id": 9101, "status": "active"})
        check("a shop the platform suspended can't be resumed by its owner",
              r.status == 409 and (await repo.get_reseller(rid)).status == Reseller.SUSPENDED)
        # ── what each shop owes its built-in customers (read by the wallet service) ──
        from sqlalchemy import update as sql_update
        from app.db import session_factory
        from app.models import Payment
        resp = await client.get("/internal/reserves", headers={"X-Provision-Secret": "wrong"})
        check("the figures need the shared secret too", resp.status == 403)
        resp = await client.get("/internal/reserves", headers=H)
        check("a shop nobody paid with built-in payments is not listed", resp.status == 200
              and all(x["shop_id"] != rid for x in (await resp.json())["shops"]))
        m1 = await repo.get_or_create_member(rid, 88001, None, None)
        m2 = await repo.get_or_create_member(rid, 88002, None, None)
        m3 = await repo.get_or_create_member(rid, 88003, None, None)
        paid_rows = {}
        for member, amount, deposit in ((m1, "30", "9301"), (m2, "10", "9302")):
            row = await repo.create_payment(rid, member.id, "numberhub", D(amount))
            await repo.attach_invoice(row.id, deposit, None, None)
            await repo.credit_payment(row.id, D(amount))
            paid_rows[deposit] = row.id
        await repo.member_adjust(rid, m3.id, D("25"))        # a balance added by hand

        async def read(query=""):
            got = (await (await client.get("/internal/reserves" + query, headers=H)).json())["shops"]
            return next(x for x in got if x["shop_id"] == rid)

        async def spend(member, price):
            """An order of this customer is charged, the way a delivered code charges it."""
            o = await repo.create_order_with_hold(
                reseller_id=rid, member_id=member.id, service="wa", service_name="WhatsApp", country="187",
                country_name="Testland", country_iso=None, member_price=D(price), markup_pct_at_buy=D("30"),
                ceiling_at_buy=D(price))
            async with session_factory() as s:
                await s.execute(sql_update(Order).where(Order.id == o.id).values(status="received"))
                await s.commit()
            return await repo.settle(o.id, True)

        fig = await read()
        check("two customers paid $40 with built-in payments and hold it all",
              fig["owner_id"] == 9101 and fig["customers_hold"] == "40.00" and fig["deposits_seen"] == "40.00", str(fig))
        check("…and each payment the shop gave out is named with NumberHub's deposit number and the amount",
              sorted((x["id"], x["amount"]) for x in fig["credited"]) == [(9301, "30.00"), (9302, "10.00")], str(fig))
        check("the owner cannot take a customer's paid-in money away to lower what the shop owes",
              not await repo.member_adjust(rid, m1.id, D("-12")) and (await read())["customers_hold"] == "40.00")
        check("the customer spends $12", await spend(m1, "12"))
        fig = await read()
        check("…customers hold $28 of the $40 paid in", fig["customers_hold"] == "28.00" and fig["deposits_seen"] == "40.00",
              str(fig))
        await repo.member_adjust(rid, m2.id, D("50"))
        check("a balance added by hand on top is not counted as paid in", (await read())["customers_hold"] == "28.00")
        pending = await repo.create_payment(rid, m3.id, "numberhub", D("9"))
        await repo.attach_invoice(pending.id, "9399", None, None)
        fig = await read()
        check("a payment that is not paid yet counts for nothing and is not named",
              fig["deposits_seen"] == "40.00" and len(fig["credited"]) == 2
              and (await repo.get_payment(pending.id)).status == Payment.PENDING)
        added = await repo.credit_payment_more(paid_rows["9302"], D("10"), D("11.50"))
        fig = await read()
        check("more arrives on a credited payment: it is named with its new amount, and the customer holds it",
              added == D("1.50") and (9302, "11.50") in [(x["id"], x["amount"]) for x in fig["credited"]]
              and fig["customers_hold"] == "29.50", str(fig))
        async with session_factory() as s:
            await s.execute(sql_update(Payment).where(Payment.id == paid_rows["9301"])
                            .values(paid_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)))
            await s.commit()
        fig, wide = await read(), await read("?days=30")
        check("a payment given out ten days ago is no longer named by default, and is with ?days=30",
              [x["id"] for x in fig["credited"]] == [9302] and sorted(x["id"] for x in wide["credited"]) == [9301, 9302]
              and fig["customers_hold"] == wide["customers_hold"] == "29.50")
        check("a days value that is not a number reads as the default",
              [x["id"] for x in (await read("?days=abc"))["credited"]] == [9302])
        settings.provision_secret = "short"
        check("a short secret keeps the listener off", await provision.start_server() is None)
    finally:
        await client.close()
        rt.make_bot, rt.start, rt.restart, provision.NumberHub, settings.provision_secret = orig
        await forget_paid_in()


async def test_custom_prices():
    """Owner feedback 2026-10-03: one percent for every number made expensive
    numbers too dear. Own price per app or app + country, and a profit cap."""
    print("custom prices")
    from types import SimpleNamespace as NS
    from app.bots.reseller import build_router
    from app.models import PriceRule
    sp = selling.shop_price
    shop = NS(markup_pct=D("30"), max_profit=None)
    rule = lambda svc, cty, mode, v: {(svc, cty): NS(mode=mode, value=D(v))}  # noqa: E731
    check("no rule: NumberHub's price + the shop's commission", sp(shop, {}, "wa", "187", D("1.00")) == D("1.30"))
    check("a profit cap holds the commission down on expensive numbers",
          sp(NS(markup_pct=D("30"), max_profit=D("0.10")), {}, "wa", "187", D("2.00")) == D("2.10")
          and sp(NS(markup_pct=D("30"), max_profit=D("0.10")), {}, "wa", "187", D("0.20")) == D("0.26"))
    check("an app's own commission", sp(shop, rule("wa", "", "pct", "10"), "wa", "187", D("1.00")) == D("1.10"))
    check("…for that app only", sp(shop, rule("wa", "", "pct", "10"), "tg", "187", D("1.00")) == D("1.30"))
    check("a fixed price for the app in one country",
          sp(shop, rule("wa", "187", "fixed", "1.05"), "wa", "187", D("1.00")) == D("1.05"))
    both = {**rule("wa", "", "pct", "50"), **rule("wa", "187", "fixed", "1.05")}
    check("the most specific rule wins (country over app)",
          sp(shop, both, "wa", "187", D("1.00")) == D("1.05") and sp(shop, both, "wa", "6", D("1.00")) == D("1.50"))
    check("a fixed price never sells below NumberHub's price (the owner never pays)",
          sp(shop, rule("wa", "187", "fixed", "0.80"), "wa", "187", D("1.00")) == D("1.00"))
    check("the cap does not cut an owner's own fixed price",
          sp(NS(markup_pct=D("30"), max_profit=D("0.05")), rule("wa", "", "fixed", "3"), "wa", "1", D("1")) == D("3.00"))

    api = FakeNumberHub()
    r = await make_reseller(api, bot_id=7951)
    selling.forget_prices(r.id)
    await repo.set_price_rule(r.id, "wa", "187", PriceRule.FIXED, D("0.33"), "WhatsApp", "USA")
    await repo.set_price_rule(r.id, "wa", "", PriceRule.PCT, D("10"), "WhatsApp")
    rows = {x["country"]: x for x in await selling.countries(r, "wa", fresh=True)}
    check("the country list shows the custom prices",
          rows["187"]["member_price"] == D("0.33") and rows["6"]["member_price"] == D("0.28")
          and rows["0"]["member_price"] == D("0.55"), str({k: v["member_price"] for k, v in rows.items()}))
    tg = await selling.countries(r, "tg", fresh=True)
    check("other apps keep the shop's commission", tg[0]["member_price"] == D("1.43"))

    m = await member(r, 9301, "5")
    order = await selling.buy(r, m, "wa", "187", shown_price=D("0.33"))
    fresh_m = await repo.get_member(m.id)
    check("a purchase holds the custom price and keeps the ceiling it sent",
          order.member_price == D("0.33") and order.ceiling_at_buy == D("0.30") and fresh_m.held == D("0.33"))

    # A lost reply: recovery must replay the SAME body (max_price = the stored
    # ceiling); rebuilding it from a custom price would send another body and
    # NumberHub would refuse it as an idempotency conflict.
    await repo.set_price_rule(r.id, "wa", "6", PriceRule.FIXED, D("0.40"), "WhatsApp", "Indonesia")
    selling.forget_prices(r.id)
    api.lose_replies = 50          # every retry of the request loses its reply too
    try:
        await selling.buy(r, m, "wa", "6", shown_price=D("0.40"))
        check("lost reply raises processing", False)
    except SellError as exc:
        check("lost reply: processing, the order waits for recovery", exc.reason == "processing")
    api.lose_replies = 0
    import datetime as _dt
    from sqlalchemy import update as _update
    from app.db import session_factory
    async with session_factory() as s:
        await s.execute(_update(Order).where(Order.member_id == m.id, Order.status == Order.BUYING)
                        .values(created_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5)))
        await s.commit()
    await selling.recover_buying()
    async with session_factory() as s:
        from sqlalchemy import select as _select
        mine = list((await s.execute(_select(Order).where(Order.member_id == m.id, Order.country == "6"))).scalars())
    check("…recovery replays the stored ceiling and finds the number (no conflict)",
          mine and mine[0].nh_id is not None and mine[0].status != Order.BUYING and mine[0].member_price == D("0.40"),
          str([(o.status, o.nh_id) for o in mine]))

    # NumberHub's real price is higher than the list: the member sees THIS
    # route's custom price at the real price, not the plain commission.
    api.true_reserve[("wa", "0")] = "0.70"
    await repo.set_price_rule(r.id, "wa", "0", PriceRule.PCT, D("5"), "WhatsApp", "Russia")
    selling.forget_prices(r.id)
    api.countries["wa"][2]["in_stock"] = True
    try:
        await selling.buy(r, m, "wa", "0", shown_price=D("0.53"))
        check("price changed", False)
    except SellError as exc:
        check("price went up: the new price is the route's own rule (0.70 + 5%)",
              exc.reason == "price_changed" and exc.price == D("0.74"), str((exc.reason, exc.price)))

    print("custom prices: the admin panel")
    session = FakeSession()
    bot = fake_bot(session)
    runtime._bots[r.id] = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(r.id))
    for rid_ in [x.id for x in await repo.price_rules(r.id)]:
        await repo.delete_price_rule(r.id, rid_)
    selling.forget_prices(r.id)
    await dp.feed_update(bot, msg(OWNER, "/admin"))
    check("the admin panel has 🏷 Custom prices", button(session, "Custom prices") is not None)
    await dp.feed_update(bot, tap(OWNER, button(session, "Custom prices")))
    check("…which explains the rules and that a fixed price never sells at a loss",
          "Custom prices" in session.last_text() and "never sells below NumberHub's price" in session.last_text()
          and "No custom prices yet" in session.last_text())
    await dp.feed_update(bot, tap(OWNER, button(session, "Add a custom price")))
    check("pick an app: popular apps as buttons", button(session, "WhatsApp") is not None)
    await dp.feed_update(bot, msg(OWNER, "tele"))
    check("…or type its name", "Apps matching" in session.last_text() and button(session, "Telegram") is not None)
    await dp.feed_update(bot, tap(OWNER, button(session, "Telegram")))
    check("pick a country: all, or one with NumberHub's price",
          button(session, "All countries") is not None and button(session, "USA · NumberHub $1.10") is not None)
    await dp.feed_update(bot, tap(OWNER, button(session, "USA")))
    check("the value step shows NumberHub's price and today's price",
          "NumberHub's price now: <b>$1.10</b>" in session.last_text() and "$1.43" in session.last_text())
    await dp.feed_update(bot, msg(OWNER, "abc"))
    check("a bad price is refused", "Send a price like" in session.last_text())
    await dp.feed_update(bot, msg(OWNER, "$1.20"))
    check("a fixed price is saved and the result is shown",
          "now sells for <b>$1.20</b>" in session.last_text() and "you earn $0.10" in session.last_text()
          and "Telegram · USA: <b>$1.20 fixed</b>" in session.last_text())
    tg = await selling.countries(r, "tg", fresh=True)
    check("…and customers see it at once", tg[0]["member_price"] == D("1.20"))
    await dp.feed_update(bot, tap(OWNER, button(session, "Add a custom price")))
    await dp.feed_update(bot, tap(OWNER, button(session, "WhatsApp")))
    await dp.feed_update(bot, tap(OWNER, button(session, "All countries")))
    check("all countries: NumberHub's range is shown",
          "NumberHub's prices for WhatsApp: <b>$0.25</b> to <b>$0.70</b>" in session.last_text(), session.last_text()[:200])
    await dp.feed_update(bot, msg(OWNER, "5%"))
    check("a commission for one app", "WhatsApp · all countries: <b>5% commission</b>" in session.last_text())
    await dp.feed_update(bot, tap(OWNER, button(session, "Add a custom price")))
    await dp.feed_update(bot, tap(OWNER, button(session, "WhatsApp")))
    await dp.feed_update(bot, tap(OWNER, button(session, "USA")))
    await dp.feed_update(bot, msg(OWNER, "0.10"))
    check("below NumberHub's price: saved, with a plain warning that it sells at NumberHub's price",
          "below NumberHub's price" in session.last_text() and "now sells for <b>$0.30</b>" in session.last_text())
    await dp.feed_update(bot, tap(OWNER, button(session, "Profit cap")))
    await dp.feed_update(bot, msg(OWNER, "0.05"))
    fresh_r = await repo.get_reseller(r.id)
    check("a profit cap", fresh_r.max_profit == D("0.05") and "at most <b>$0.05</b>" in session.last_text())
    await dp.feed_update(bot, tap(OWNER, button(session, "Profit cap")))
    await dp.feed_update(bot, msg(OWNER, "-"))
    check("…and removing it", (await repo.get_reseller(r.id)).max_profit is None)
    n_before = len(await repo.price_rules(r.id))
    await dp.feed_update(bot, tap(OWNER, button(session, "🗑 1")))
    check("🗑 removes a custom price", len(await repo.price_rules(r.id)) == n_before - 1
          and "removed" in session.last_text())
    await dp.feed_update(bot, tap(ALICE + 500, "a:prices:"))
    check("a customer can't open the price screen", "Custom prices" not in (session.last_text() or "")
          or session.requests[-1].__class__.__name__ == "AnswerCallbackQuery")
    await dp.feed_update(bot, tap(OWNER, "a:pr_del:999999"))
    check("removing a rule that isn't there is harmless", "Custom prices" in session.last_text())

    print("custom prices: an older database")
    from sqlalchemy import text as _text
    from app.db import engine, init_db as _init
    import sqlite3 as _sqlite
    from app import db as _db
    want = ("ceiling_at_buy", "max_profit")
    if _sqlite.sqlite_version_info >= (3, 35):
        async with engine.begin() as conn:
            await conn.execute(_text("ALTER TABLE orders DROP COLUMN ceiling_at_buy"))
            await conn.execute(_text("ALTER TABLE resellers DROP COLUMN max_profit"))
        await _init()
    else:
        # An older SQLite cannot drop a column. The same start-up step is proven with
        # columns no table has yet (the ORM never reads them).
        want = ("zz_added_later_a", "zz_added_later_b")
        saved = _db._ADDED_COLUMNS
        _db._ADDED_COLUMNS = saved + (("orders", want[0], "NUMERIC(12, 2)"), ("resellers", want[1], "TEXT"))
        try:
            await _init()
            await _init()               # a second start must not try to add them again
        finally:
            _db._ADDED_COLUMNS = saved
    async with engine.begin() as conn:
        cols = {row[1] for row in (await conn.execute(_text("PRAGMA table_info(orders)"))).all()}
        rcols = {row[1] for row in (await conn.execute(_text("PRAGMA table_info(resellers)"))).all()}
        pcols = {row[1] for row in (await conn.execute(_text("PRAGMA table_info(payments)"))).all()}
        mcols = {row[1] for row in (await conn.execute(_text("PRAGMA table_info(members)"))).all()}
    check("start-up adds the new columns to a database from before",
          want[0] in cols and want[1] in rcols)
    check("every column a later release added is in the database",
          all(col in {"resellers": rcols, "orders": cols, "payments": pcols, "members": mcols}[tbl]
              for tbl, col, _kind in _db._ADDED_COLUMNS))


async def test_picker_pages():
    """Owner feedback 2026-10-03: only 16 countries were listed, the rest looked
    unavailable. Every country is reachable now, page by page."""
    print("custom prices: every country")
    from app.bots.admin import country_picker
    api = FakeNumberHub()
    api.countries["tg"] = [{"country": str(i), "name": f"Land {i:02d}", "flag": "US", "price": "0.50",
                            "price_max": "0.50", "in_stock": i % 3 != 0, "rate": 30, "rate_low": False,
                            "rate_dead": False, "collapsed": False} for i in range(1, 46)]
    r = await make_reseller(api, bot_id=7971)
    selling.forget_prices(r.id)
    text, kb = await country_picker(r, "tg")
    labels = [b.text for row in kb.inline_keyboard for b in row]
    check("page 1: all countries, 20 countries, a page counter", "45 countries" in text
          and labels[0] == "🌍 All countries" and len([x for x in labels if "Land" in x]) == 20 and "1/3" in labels)
    _t, kb3 = await country_picker(r, "tg", page=2)
    l3 = [b.text for row in kb3.inline_keyboard for b in row]
    check("the last page has the rest, out of stock marked ⏳",
          len([x for x in l3 if "Land" in x]) == 5 and "3/3" in l3 and any("⏳" in x for x in l3))
    _t, kbq = await country_picker(r, "tg", query="land 4")
    check("typing still finds any country", len([b for row in kbq.inline_keyboard for b in row if "Land 4" in b.text]) == 6)


async def test_rank_order():
    """2026-10-04: a shop's Telegram list opened on countries that never deliver.
    NumberHub sends `rank` (its own bot's order); the shop follows it."""
    print("country order = NumberHub's rank")
    api = FakeNumberHub()
    api.countries["tg"] = [
        {"country": "6", "name": "Indonesia", "flag": "ID", "price": "0.10", "price_max": "0.10",
         "in_stock": True, "rate": 0, "rate_low": True, "rate_dead": False, "collapsed": False, "rank": 3},
        {"country": "187", "name": "USA", "flag": "US", "price": "0.80", "price_max": "0.80",
         "in_stock": True, "rate": 34, "rate_low": False, "rate_dead": False, "collapsed": False, "rank": 2},
        {"country": "39", "name": "Argentina", "flag": "AR", "price": "0.60", "price_max": "0.60",
         "in_stock": True, "rate": 21, "rate_low": False, "rate_dead": False, "collapsed": False, "rank": 1},
        {"country": "0", "name": "Russia", "flag": "RU", "price": "0.50", "price_max": "0.50",
         "in_stock": False, "rate": None, "rate_low": False, "rate_dead": False, "collapsed": False, "rank": 4},
    ]
    r = await make_reseller(api, bot_id=7981)
    rows = await selling.countries(r, "tg", fresh=True)
    check("the shop lists countries in NumberHub's order (not by its own rate guess)",
          [x["name"] for x in rows] == ["Argentina", "USA", "Indonesia", "Russia"], str([x["name"] for x in rows]))
    for x in api.countries["tg"]:
        x.pop("rank")
    rows = await selling.countries(r, "tg", fresh=True)
    check("an older server without `rank`: stock and rate still decide",
          [x["name"] for x in rows] == ["USA", "Argentina", "Indonesia", "Russia"], str([x["name"] for x in rows]))
    runtime._bots.pop(r.id, None)


async def test_dead_token():
    """2026-10-04: two paused shops polled Telegram every 5 s all day, one with a
    revoked token (Unauthorized), one whose token another service used with a
    webhook (Conflict): 2,400 errors. The shop now stops polling and is marked
    for reconnecting; its orders keep syncing; sending the token again revives it."""
    print("dead or taken bot token")
    from aiogram.exceptions import TelegramConflictError, TelegramUnauthorizedError
    from aiogram.methods import GetMe, GetUpdates
    from app import provision
    api = FakeNumberHub()
    r = await make_reseller(api, bot_id=7991)
    halted = []

    async def fake_halt(rid):
        halted.append(rid)
    real_halt, runtime._halt_polling = runtime._halt_polling, fake_halt

    async def unauthorized(bot, method):
        raise TelegramUnauthorizedError(method=method, message="Unauthorized")
    watch = runtime._TokenWatch(r.id)
    try:
        await watch(unauthorized, None, GetUpdates())
    except TelegramUnauthorizedError:
        pass
    await asyncio.sleep(0.05)
    fresh = await repo.get_reseller(r.id)
    check("a revoked token stops polling and marks the shop", halted == [r.id] and fresh.status == "token_invalid",
          fresh.status)
    check("its orders keep syncing (the NumberHub client stays)", selling.client_for(r.id) is not None)
    check("the hosting side shows it as 'needs reconnecting'",
          provision._shop_json(fresh)["status"] == Reseller.KEY_INVALID)

    r2 = await make_reseller(api, bot_id=7992)
    clock = {"t": 0.0}
    w2 = runtime._TokenWatch(r2.id, clock=lambda: clock["t"])

    async def conflict(bot, method):
        raise TelegramConflictError(method=method, message="Conflict: terminated by other getUpdates request")

    async def ok(bot, method):
        return []

    async def poll(fn):
        try:
            await w2(fn, None, GetUpdates())
        except TelegramConflictError:
            pass
    for _ in range(runtime.CONFLICTS_BEFORE_GIVING_UP):     # spread over more than the window
        await poll(conflict)
        clock["t"] += runtime.CONFLICT_WINDOW_SEC / (runtime.CONFLICTS_BEFORE_GIVING_UP - 1) + 1
    await asyncio.sleep(0.05)
    check("conflicts spread over more than 10 minutes are tolerated (a restart overlap, a stray poll)",
          (await repo.get_reseller(r2.id)).status == Reseller.ACTIVE and r2.id not in halted)
    w2 = runtime._TokenWatch(r2.id, clock=lambda: clock["t"])
    told = []

    async def send_message(chat_id, text, **_):
        told.append((chat_id, text))
    from types import SimpleNamespace
    runtime._bots[r2.id] = SimpleNamespace(send_message=send_message)
    for _ in range(runtime.CONFLICTS_BEFORE_GIVING_UP):     # two pollers knocking each other off every 35 s
        await poll(conflict)
        clock["t"] += 5
        await poll(ok)
        clock["t"] += 30
    await asyncio.sleep(0.05)
    check(f"{runtime.CONFLICTS_BEFORE_GIVING_UP} conflicts in 10 minutes, good polls between: another service has the bot, polling stops",
          (await repo.get_reseller(r2.id)).status == "token_invalid" and r2.id in halted)
    check("the owner is told in their own bot why it stopped and what to do",
          len(told) == 1 and told[0][0] == OWNER and "Another program" in told[0][1] and "@my_shop_bot" in told[0][1])
    runtime._bots.pop(r2.id, None)
    try:
        await w2(unauthorized, None, GetMe())
    except TelegramUnauthorizedError:
        pass
    check("only getUpdates is watched (a failed send does not end the shop)", halted.count(r2.id) == 1)
    runtime._halt_polling = real_halt

    started = []
    real_make = runtime.make_bot
    runtime.make_bot = lambda token: started.append(token) or real_make(token)
    await runtime.start(await repo.get_reseller(r.id))
    check("a restart does not poll a shop waiting for its token", not started and r.id not in runtime.running())
    runtime.make_bot = real_make

    class Req(dict):
        def __init__(self, rid, body):
            super().__init__()
            self.match_info = {"id": str(rid)}
            self._body = body

        async def json(self):
            return self._body
    resp = await provision.set_status(Req(r.id, {"owner_id": OWNER, "status": "active"}))
    check("resuming is refused until the token is sent again", resp.status == 409)


async def test_rejected_key():
    """2026-10-15: a shop whose NumberHub key was revoked had no live orders, so
    only the 2-minute price warm-up used the key: 1,280 refused calls in 38 h,
    with no end. A 401 on any call now parks the shop as KEY_INVALID, and only
    selling shops are warmed."""
    print("a rejected NumberHub key parks the shop")
    api = FakeNumberHub()
    r = await make_reseller(api, bot_id=7993)
    api.reject_key = True
    api.calls.clear()
    await selling.warm_popular(await repo.get_reseller(r.id))
    first = len(api.calls)
    check("the warm-up stops at the first refusal", 1 <= first <= 2, f"{first} calls")
    check("…and parks the shop as 'key rejected'", (await repo.get_reseller(r.id)).status == Reseller.KEY_INVALID)
    for _ in range(3):
        await selling.warm_popular(await repo.get_reseller(r.id))
    check("later warm-ups send nothing with the dead key", len(api.calls) == first, f"{len(api.calls) - first} more")

    api.reject_key = False
    r2 = await make_reseller(api, bot_id=7994)
    m = await member(r2, ALICE + 900, "5")
    await selling.buy(r2, m, "wa", "187", None)
    api.reject_key = True
    selling._backoff_until.pop(r2.id, None)
    await selling.sync_reseller(await repo.get_reseller(r2.id))
    check("an order sync that hits the 401 parks the shop the same way",
          (await repo.get_reseller(r2.id)).status == Reseller.KEY_INVALID)

    r3 = await make_reseller(api, bot_id=7995)
    old = selling.client_for(r3.id)
    await repo.update_reseller(r3.id, api_key_enc=crypto.encrypt("nh_live_reconnected_" + "k" * 24),
                               status=Reseller.ACTIVE)          # the owner reconnects with a new key
    await selling.key_rejected(r3.id, old)                      # a late 401 from a request on the old key
    check("a late 401 from the old key never undoes a reconnect",
          (await repo.get_reseller(r3.id)).status == Reseller.ACTIVE)

    api.reject_key = False
    for status in (Reseller.DISABLED, Reseller.TOKEN_INVALID, Reseller.SUSPENDED, Reseller.KEY_INVALID):
        await repo.update_reseller(r3.id, status=status)
        before = len(api.calls)
        await selling.warm_popular(await repo.get_reseller(r3.id))
        check(f"a {status} shop is not warmed", len(api.calls) == before)
    for rid in (r.id, r2.id, r3.id):
        cli = selling.drop_client(rid)
        if cli is not None:
            await cli.close()


def photo_msg(uid, caption=None):
    return Update.model_validate({"update_id": next(_uid), "message": {
        "message_id": next(_uid), "date": 0, "chat": {"id": uid, "type": "private"}, "from": _user(uid, "en"),
        "photo": [{"file_id": "small-id", "file_unique_id": "s", "width": 90, "height": 90},
                  {"file_id": "BIG-file-id", "file_unique_id": "b", "width": 1280, "height": 1280}],
        **({"caption": caption} if caption else {})}})


async def test_deposits():
    """Owner feedback 2026-10-03 ("✅ Deposit #5 approved" - such a system): the
    customer asks for balance in the bot, the owner approves with one tap."""
    print("deposits")
    from aiogram.methods import EditMessageCaption, SendMessage, SendPhoto
    from app.bots.reseller import build_router
    from app.models import Deposit
    api = FakeNumberHub()
    r = await make_reseller(api, bot_id=7961)
    session = FakeSession()
    bot = fake_bot(session)
    runtime._bots[r.id] = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(r.id))
    cust = 9401
    to = lambda uid: [m for m in session.requests if isinstance(m, (SendMessage, SendPhoto))  # noqa: E731
                      and m.chat_id == uid]

    await dp.feed_update(bot, msg(cust, "/start"))
    await dp.feed_update(bot, tap(cust, button(session, "Balance")))
    check("deposits off: no Add balance button (send your ID to the seller, as before)",
          button(session, "Add balance") is None and "send your ID" in session.last_text())

    await dp.feed_update(bot, msg(OWNER, "/admin"))
    await dp.feed_update(bot, tap(OWNER, button(session, "Payments")))
    check("admin: 💳 Payments explains it and says manual deposits are off", "Off." in session.last_text())
    await dp.feed_update(bot, tap(OWNER, button(session, "Payment details")))
    await dp.feed_update(bot, msg(OWNER, "bKash: 01700000000\nBinance Pay ID: 123456789"))
    check("payment details turn deposits on", "deposits are on" in session.last_text()
          and "Binance Pay ID: 123456789" in session.last_text())
    await dp.feed_update(bot, tap(OWNER, button(session, "Minimum")))
    await dp.feed_update(bot, msg(OWNER, "2"))
    check("a minimum deposit", (await repo.get_reseller(r.id)).deposit_min == D("2"))

    await dp.feed_update(bot, msg(cust, "/start"))
    await dp.feed_update(bot, tap(cust, button(session, "Balance")))
    await dp.feed_update(bot, tap(cust, button(session, "Add balance")))
    check("the customer sees how to pay, and the minimum",
          "bKash: 01700000000" in session.last_text() and "Minimum: <b>$2.00</b>" in session.last_text())
    await dp.feed_update(bot, msg(cust, "abc"))
    check("a bad amount is refused", "Send the amount as a number" in session.last_text())
    await dp.feed_update(bot, msg(cust, "1"))
    check("below the minimum is refused", "The minimum is <b>$2.00</b>" in session.last_text())
    await dp.feed_update(bot, msg(cust, "5"))
    check("asks for the transaction ID or a screenshot", "transaction ID" in session.last_text())
    n_owner = len(to(OWNER))
    await dp.feed_update(bot, msg(cust, "TX-778899"))
    mem = await repo.get_or_create_member(r.id, cust, None, None)
    dep = await repo.pending_deposit(mem.id)
    owner_note = to(OWNER)[n_owner:]
    check("the request is saved as #1 and the customer is told",
          dep is not None and dep.number == 1 and dep.amount == D("5") and dep.proof_text == "TX-778899"
          and "Deposit #1</b> sent: $5.00" in session.last_text())
    check("the owner gets it at once with Approve / Other amount / Reject",
          owner_note and "Deposit #1" in owner_note[-1].text and "TX-778899" in owner_note[-1].text
          and any("Approve $5.00" in b.text for row in owner_note[-1].reply_markup.inline_keyboard for b in row))
    await dp.feed_update(bot, tap(cust, "n:deposit"))
    check("one deposit at a time: a second one waits for the first", "still waiting" in session.last_text())

    m_before = await repo.get_member(mem.id)
    await dp.feed_update(bot, tap(cust, f"d:ok:{dep.id}"))
    check("the customer can't approve their own deposit",
          (await repo.get_deposit(r.id, dep.id)).status == Deposit.PENDING)
    # Two taps / two devices at once: credited exactly once.
    await asyncio.gather(dp.feed_update(bot, tap(OWNER, f"d:ok:{dep.id}")),
                         dp.feed_update(bot, tap(OWNER, f"d:ok:{dep.id}")))
    m_after = await repo.get_member(mem.id)
    check("approve: +$5.00 exactly once, even on a double tap",
          m_after.balance - m_before.balance == D("5")
          and (await repo.get_deposit(r.id, dep.id)).status == Deposit.APPROVED,
          f"{m_before.balance} -> {m_after.balance}")
    check("the customer is told, with the new balance",
          any("Deposit #1 approved:</b> +$5.00" in (x.text or "") for x in to(cust)))
    await dp.feed_update(bot, tap(OWNER, f"d:ok:{dep.id}"))
    check("a late tap is told it was already approved", "already approved" in session.alerts()[-1])

    # A screenshot; the owner corrects the amount.
    await dp.feed_update(bot, tap(cust, "n:deposit"))
    await dp.feed_update(bot, msg(cust, "10"))
    await dp.feed_update(bot, photo_msg(cust))
    dep2 = await repo.pending_deposit(mem.id)
    photos = [x for x in to(OWNER) if isinstance(x, SendPhoto)]
    check("a screenshot is kept and sent to the owner as the photo",
          dep2 is not None and dep2.proof_photo == "BIG-file-id" and photos and photos[-1].photo == "BIG-file-id"
          and "Deposit #2" in photos[-1].caption)
    await dp.feed_update(bot, tap(OWNER, f"d:amt:{dep2.id}"))
    await dp.feed_update(bot, msg(OWNER, "9.50"))
    m3 = await repo.get_member(mem.id)
    d2 = await repo.get_deposit(r.id, dep2.id)
    check("other amount: the owner credits what really arrived",
          d2.status == Deposit.APPROVED and d2.credited == D("9.50") and m3.balance - m_after.balance == D("9.50"))

    # Rejected.
    await dp.feed_update(bot, tap(cust, "n:deposit"))
    await dp.feed_update(bot, msg(cust, "3"))
    await dp.feed_update(bot, msg(cust, "fake-id"))
    dep3 = await repo.pending_deposit(mem.id)
    await dp.feed_update(bot, tap(OWNER, f"d:no:{dep3.id}"))
    m4 = await repo.get_member(mem.id)
    check("reject: nothing added, the customer is told",
          (await repo.get_deposit(r.id, dep3.id)).status == Deposit.REJECTED and m4.balance == m3.balance
          and any("Deposit #3 (" in (x.text or "") and "not approved" in x.text for x in to(cust)))

    # The admin list: what waits, with approve buttons.
    await dp.feed_update(bot, tap(cust, "n:deposit"))
    await dp.feed_update(bot, msg(cust, "4"))
    await dp.feed_update(bot, msg(cust, "TX-1"))
    await dp.feed_update(bot, msg(OWNER, "/admin"))
    check("the dashboard shows what waits", "Deposits waiting for you: <b>1</b>" in session.last_text()
          and button(session, "Payments (1)") is not None)
    await dp.feed_update(bot, tap(OWNER, button(session, "Payments")))
    approve4 = button(session, "✅ #4 $4.00")
    check("…and the list approves from there too", approve4 is not None)
    for _ in range(3):
        await dp.feed_update(bot, tap(OWNER, approve4))
    check("…once", (await repo.get_member(mem.id)).balance - m4.balance == D("4"))

    # Limits and switching off.
    for i in range(5):
        await repo.create_deposit(r.id, mem.id, D("1"), f"x{i}", None)
        p = await repo.pending_deposit(mem.id)
        await repo.decide_deposit(r.id, p.id, False)
    await dp.feed_update(bot, tap(cust, "n:deposit"))
    check("at most 5 requests a day", "several deposits today" in session.last_text())
    await dp.feed_update(bot, msg(OWNER, "/admin"))
    await dp.feed_update(bot, tap(OWNER, "a:dep_info:"))
    await dp.feed_update(bot, msg(OWNER, "-"))
    check("'-' turns deposits off", (await repo.get_reseller(r.id)).deposit_info is None)
    _ = EditMessageCaption


def url_button(session: FakeSession, contains: str) -> str | None:
    mk = session.last_markup()
    for row in (mk.inline_keyboard if mk else []):
        for b in row:
            if contains in b.text and b.url:
                return b.url
    return None


async def test_auto_payments():
    """Owner 2026-11-15 (#61): customers top up by crypto through the shop owner's
    OWN OxaPay / @CryptoBot, and their balance goes up by itself."""
    print("automatic payments (the owner's own OxaPay / @CryptoBot)")
    from aiogram.methods import DeleteMessage, SendMessage
    from app import payments
    from app.bots.callbacks import Pay
    from app.bots.reseller import build_router, error_text
    from app.models import MemberTx, Payment
    from fakes import FakeGateways
    from sqlalchemy import func, select
    gw = FakeGateways()
    payments.TRANSPORT = gw.transport()
    api = FakeNumberHub()
    r = await make_reseller(api, bot_id=86061)
    session = FakeSession()
    bot = fake_bot(session)
    runtime._bots[r.id] = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(r.id))
    cust = 9501
    to = lambda uid: [m.text or "" for m in session.requests if isinstance(m, SendMessage) and m.chat_id == uid]  # noqa: E731

    # ── the owner connects OxaPay ──
    await dp.feed_update(bot, msg(OWNER, "/admin"))
    check("dashboard: automatic payments are off until a gateway is connected",
          "Automatic payments: <b>off</b>" in session.last_text())
    await dp.feed_update(bot, tap(OWNER, button(session, "Payments")))
    check("💳 Payments offers both gateways", button(session, "Connect 🪙 OxaPay") is not None
          and button(session, "Connect 🤖 @CryptoBot") is not None)
    await dp.feed_update(bot, tap(OWNER, button(session, "Connect 🪙 OxaPay")))
    check("…with how to get the key", "Merchant API key" in session.last_text())
    n_del = sum(isinstance(x, DeleteMessage) for x in session.requests)
    await dp.feed_update(bot, msg(OWNER, "OXA-WRONG-KEY-999"))
    check("a wrong key is refused, and the message holding it is deleted",
          "didn't accept" in session.last_text() and (await repo.get_reseller(r.id)).oxapay_key_enc is None
          and sum(isinstance(x, DeleteMessage) for x in session.requests) == n_del + 1)
    await dp.feed_update(bot, msg(OWNER, gw.ox_key))
    fresh = await repo.get_reseller(r.id)
    check("the right key is checked with a sandbox invoice (no real payment) and stored encrypted",
          fresh.oxapay_key_enc and gw.ox_key not in fresh.oxapay_key_enc
          and crypto.decrypt(fresh.oxapay_key_enc) == gw.ox_key
          and any(v["sandbox"] for v in gw.ox.values()) and "OxaPay connected" in session.last_text())

    # ── a customer tops up $10 ──
    await dp.feed_update(bot, msg(cust, "/start"))
    await dp.feed_update(bot, tap(cust, button(session, "Balance")))
    check("balance: 'tap 💳 Add balance' instead of 'send your ID to the seller'",
          "tap 💳 Add balance" in session.last_text() and "send your ID" not in session.last_text()
          and button(session, "Add balance") is not None)
    await dp.feed_update(bot, tap(cust, button(session, "Add balance")))
    check("amounts to pick", button(session, "$10.00") is not None and button(session, "Other amount") is not None)
    await dp.feed_update(bot, tap(cust, button(session, "$10.00")))
    link = url_button(session, "Pay $10.00")
    p1 = (await repo.open_payments((await repo.get_or_create_member(r.id, cust, None, None)).id))[0]
    check("one gateway: straight to a $10.00 payment link from the owner's OxaPay",
          link and link.startswith("https://pay.oxapay.com/") and p1.amount == D("10")
          and gw.ox[p1.invoice_id]["fee_paid_by_payer"] == 1 and not gw.ox[p1.invoice_id]["sandbox"]
          and "within 60 minutes" in session.last_text())
    mem = await repo.get_or_create_member(r.id, cust, None, None)
    await dp.feed_update(bot, tap(cust, Pay(a="chk", id=p1.id).pack()))
    check("'I've paid' before it arrives: not yet, nothing added",
          "Not received yet" in session.alerts()[-1] and (await repo.get_member(mem.id)).balance == 0)
    gw.ox[p1.invoice_id]["status"] = "Paid"
    # The poller twice and the customer's button at the same time: credited ONCE.
    await asyncio.gather(payments.poll_all(), payments.poll_all(),
                         dp.feed_update(bot, tap(cust, Pay(a="chk", id=p1.id).pack())))
    after = await repo.get_member(mem.id)
    async with repo.session_factory() as s:
        n_tx = await s.scalar(select(func.count(MemberTx.id)).where(MemberTx.member_id == mem.id,
                                                                     MemberTx.kind == "payment"))
    check("paid: +$10.00 exactly once, even with the poller and the button racing",
          after.balance == D("10") and n_tx == 1 and (await repo.get_payment(p1.id)).status == Payment.PAID,
          f"balance={after.balance} tx={n_tx}")
    check("the customer is told, with the new balance",
          any("Payment received:</b> +$10.00" in x and "$10.00" in x for x in to(cust))
          or "Payment received:</b> +$10.00" in session.last_text())
    check("the owner is told the money is in THEIR OxaPay balance",
          any("Payment received:</b> +$10.00" in x and "OxaPay balance" in x for x in to(OWNER)))
    await dp.feed_update(bot, tap(cust, Pay(a="chk", id=p1.id).pack()))
    check("a later check: already on your balance", "already on your balance" in session.alerts()[-1])
    await dp.feed_update(bot, tap(BOB, Pay(a="chk", id=p1.id).pack()))
    check("another customer can't touch someone else's link", "old message" in session.alerts()[-1])

    # ── both gateways: the customer chooses ──
    await dp.feed_update(bot, msg(OWNER, "/admin"))
    await dp.feed_update(bot, tap(OWNER, button(session, "Payments")))
    await dp.feed_update(bot, tap(OWNER, button(session, "Connect 🤖 @CryptoBot")))
    await dp.feed_update(bot, msg(OWNER, gw.cb_token))
    check("@CryptoBot connected after getMe accepted the token",
          "@CryptoBot connected" in session.last_text() and "My shop app" in session.last_text())
    await dp.feed_update(bot, tap(cust, "n:deposit"))
    await dp.feed_update(bot, tap(cust, button(session, "Other amount")))
    await dp.feed_update(bot, msg(cust, "0.50"))
    check("a typed amount below the minimum is refused", "Send an amount from <b>$1.00</b>" in session.last_text())
    await dp.feed_update(bot, msg(cust, "5000"))
    check("…and above the maximum", "to <b>$1000.00</b>" in session.last_text())
    await dp.feed_update(bot, msg(cust, "15"))
    check("two gateways: the customer picks one", button(session, "BTC, ETH, USDT") is not None
          and button(session, "@CryptoBot wallet") is not None and "<b>$15.00</b>" in session.last_text())
    await dp.feed_update(bot, tap(cust, button(session, "@CryptoBot wallet")))
    cb_link = url_button(session, "Pay $15.00")
    p2 = (await repo.open_payments(mem.id))[0]
    inv = gw.cb[int(p2.invoice_id)]
    check("@CryptoBot: an invoice in USD (the customer picks the coin) for $15.00",
          cb_link and cb_link.startswith("https://t.me/CryptoBot") and inv["currency_type"] == "fiat"
          and inv["fiat"] == "USD" and inv["amount"] == "15.00" and p2.provider == payments.CRYPTOBOT)
    inv["status"] = "expired"
    await payments.poll_all()
    check("an expired link: closed, nothing added",
          (await repo.get_payment(p2.id)).status == Payment.EXPIRED and (await repo.get_member(mem.id)).balance == D("10"))
    await dp.feed_update(bot, tap(cust, Pay(a="chk", id=p2.id).pack()))
    check("…and its check button says so", "expired" in session.alerts()[-1])

    # ── forged buttons, limits, refusals ──
    n_before = len(gw.ox)
    await dp.feed_update(bot, tap(cust, Pay(a="via", g="ox", c=1).pack()))
    check("a forged one-cent button makes no link", len(gw.ox) == n_before and "How much" in session.last_text())
    for _ in range(3):
        await dp.feed_update(bot, tap(cust, Pay(a="via", g="ox", c=500).pack()))
    n_open = len(await repo.open_payments(mem.id))
    await dp.feed_update(bot, tap(cust, Pay(a="via", g="ox", c=500).pack()))
    check("at most 3 open links per customer", n_open == 3 and len(await repo.open_payments(mem.id)) == 3
          and "several payments open" in session.last_text())
    await dp.feed_update(bot, tap(cust, "n:deposit"))
    check("the amount screen shows the open link to pay", url_button(session, "Pay $5.00") is not None)
    for p in await repo.open_payments(mem.id):
        await repo.close_payment(p.id, Payment.EXPIRED)
    gw.refuse_create = True
    n_owner = len(to(OWNER))
    await dp.feed_update(bot, tap(cust, Pay(a="via", g="ox", c=700).pack()))
    await dp.feed_update(bot, tap(cust, Pay(a="via", g="ox", c=700).pack()))
    owner_warns = [x for x in to(OWNER)[n_owner:] if "refused to make a payment link" in x]
    async with repo.session_factory() as s:
        failed = await s.scalar(select(func.count(Payment.id)).where(Payment.member_id == mem.id,
                                                                      Payment.status == Payment.FAILED))
    check("the gateway refuses: the customer is told, the owner warned once, the row marked failed",
          "not available right now" in session.last_text() and len(owner_warns) == 1 and failed == 2)
    gw.refuse_create = False

    # ── accepted short payment, late link, owner removes a gateway ──
    p3 = await payments.open_link(await repo.get_reseller(r.id), mem.id, payments.OXAPAY, D("8"))
    gw.ox[p3.invoice_id]["status"] = "Manual_Accept"
    n_owner = len(to(OWNER))
    await payments.poll_all()
    check("an underpayment the owner accepted in OxaPay is left for the owner to add by hand",
          (await repo.get_payment(p3.id)).status == Payment.MANUAL and (await repo.get_member(mem.id)).balance == D("10")
          and any("add what really arrived" in x for x in to(OWNER)[n_owner:]))
    p4 = await payments.open_link(await repo.get_reseller(r.id), mem.id, payments.OXAPAY, D("6"))
    async with repo.session_factory() as s:
        from sqlalchemy import update as sa_update
        await s.execute(sa_update(Payment).where(Payment.id == p4.id).values(
            expires_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=7)))
        await s.commit()
    await payments.poll_all()
    check("a link still unpaid hours after it expired is closed",
          (await repo.get_payment(p4.id)).status == Payment.EXPIRED)
    await dp.feed_update(bot, msg(OWNER, "/admin"))
    check("dashboard: both gateways and 30-day totals",
          "OxaPay + @CryptoBot" in session.last_text() and "1 paid, $10.00 in 30 days" in session.last_text())
    await dp.feed_update(bot, tap(OWNER, button(session, "Payments")))
    check("💳 Payments lists the recent payments", "✅ $10.00 · OxaPay" in session.last_text())
    await dp.feed_update(bot, tap(OWNER, button(session, "Remove OxaPay")))
    gone = await repo.get_reseller(r.id)
    check("removing OxaPay forgets the key, @CryptoBot stays",
          gone.oxapay_key_enc is None and payments.connected(gone) == [payments.CRYPTOBOT])
    no_credit = SellError("no_credit", price=D("0.39"))
    check("not enough balance: 'tap 💳 Add balance', not 'send your ID'",
          "Add balance" in error_text(no_credit, gone, after, "en")
          and "send your ID" not in error_text(no_credit, gone, after, "en"))
    await dp.feed_update(bot, tap(OWNER, button(session, "Remove @CryptoBot")))
    await dp.feed_update(bot, tap(cust, button(session, "Balance") or "n:balance"))
    check("with no gateway left, the balance screen is back to 'send your ID'",
          "send your ID" in session.last_text())
    payments.TRANSPORT = None


async def test_builtin_payments():
    """Owner 2026-11-23 (#99): built-in USDT payments. The customer pays to an
    address NumberHub opens with the shop's own key; the money lands in the shop's
    NumberHub wallet and the same amount on the customer's balance, exactly once."""
    print("built-in USDT payments (into the shop's NumberHub wallet)")
    import json
    from aiogram.methods import SendMessage
    from app import payments
    from app.bots.callbacks import Pay
    from app.bots.reseller import build_router
    from app.config import settings
    from app.models import MemberTx, Payment
    from fakes import FakeGateways
    from sqlalchemy import func, select, update
    payments.TRANSPORT = FakeGateways().transport()     # leftovers of other tests never reach a real gateway
    payments.ASK_EVERY_FRESH = payments.ASK_EVERY_OLD = payments.ASK_EVERY_LATE = 0.0   # no waiting between asks here
    payments._methods_cache = None
    api = FakeNumberHub(wallet="0.00")
    r = await make_reseller(api, bot_id=86099)
    session = FakeSession()
    bot = fake_bot(session)
    runtime._bots[r.id] = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(r.id))
    cust, other = 9601, 9602
    to = lambda uid: [m.text or "" for m in session.requests if isinstance(m, SendMessage) and m.chat_id == uid]  # noqa: E731

    async def refused(coro) -> str:
        try:
            await coro
        except payments.PayError as exc:
            return str(exc) or "refused"
        return ""

    async def to_amounts(uid):
        await dp.feed_update(bot, msg(uid, "/start"))
        await dp.feed_update(bot, tap(uid, button(session, "Balance")))
        await dp.feed_update(bot, tap(uid, button(session, "Add balance")))

    # ── off until the owner turns it on ──
    check("off by default: nothing changes for a shop", payments.connected(r) == [] and not payments.builtin_on(r))
    await dp.feed_update(bot, msg(cust, "/start"))
    await dp.feed_update(bot, tap(cust, button(session, "Balance")))
    check("…and the customer has no Add balance button", button(session, "Add balance") is None)
    check("…and opening one by hand is refused",
          "off" in await refused(payments.open_link(r, 1, payments.NUMBERHUB, D("5"), "usdt_bsc")))

    # ── the owner turns it on with one tap ──
    await dp.feed_update(bot, msg(OWNER, "/admin"))
    await dp.feed_update(bot, tap(OWNER, button(session, "Payments")))
    check("💳 Payments explains built-in crypto and offers the switch",
          "Built-in crypto" in session.last_text() and "your NumberHub wallet" in session.last_text()
          and "BTC" in session.last_text() and button(session, "Turn on built-in crypto") is not None)
    await dp.feed_update(bot, tap(OWNER, button(session, "Turn on built-in crypto")))
    r = await repo.get_reseller(r.id)
    check("one tap turns it on, nothing to connect", r.nh_pay == 1 and payments.connected(r) == [payments.NUMBERHUB]
          and button(session, "Turn off built-in crypto") is not None and "are on" in session.last_text())
    await dp.feed_update(bot, msg(OWNER, "/admin"))
    check("the dashboard names it", "Built-in crypto" in session.last_text())

    # ── a customer pays $10 on BEP20 ──
    await to_amounts(cust)
    await dp.feed_update(bot, tap(cust, button(session, "$10.00")))
    mk = session.last_markup()
    offered = [b.text.split()[-1] for row in mk.inline_keyboard for b in row][:-1]
    check("the customer picks a coin: every coin the shop can take, the most used first",
          offered == ["USDT", "ETH", "LTC", "USDC"] and "how do you want to pay" in session.last_text(), str(offered))
    check("…Bitcoin is not offered for $10: it cannot take less than $25", button(session, "BTC") is None)
    await dp.feed_update(bot, tap(cust, button(session, "USDT")))
    check("…then the network, when the coin has more than one", button(session, "BEP20") is not None
          and button(session, "TRC20") is not None and button(session, "BTC") is None)
    await dp.feed_update(bot, tap(cust, button(session, "BEP20")))
    mem = await repo.get_or_create_member(r.id, cust, None, None)
    p1 = (await repo.open_payments(mem.id))[0]
    top = api.topups[int(p1.invoice_id)]
    text = session.last_text()
    check("an address, the exact USDT, the network and the time; no link",
          top["address"] in text and "<code>10.20</code> USDT on" in text and "BNB Smart Chain (BEP20)" in text
          and re.search(r"Valid for (179|180) minutes", text) and "is lost" in text
          and url_button(session, "Pay") is None and p1.pay_url is None and p1.provider == payments.NUMBERHUB,
          text[:300])
    check("the deposit was opened for this customer alone, for three hours",
          top["reference"] == f"s{r.id}-m{mem.id}-p{p1.id}" and top["lifetime"] == 180
          and top["amount"] == D("10.00") and top["method"] == "usdt_bsc")
    check("the customer's Telegram id and name never leave the shop",
          str(cust) not in json.dumps(api.topup_bodies) and "user" not in json.dumps(api.topup_bodies).lower())
    await dp.feed_update(bot, tap(cust, Pay(a="chk", id=p1.id).pack()))
    check("'I've paid' before it arrives: not yet, nothing added",
          "Not received yet" in session.alerts()[-1] and (await repo.get_member(mem.id)).balance == 0)
    await to_amounts(cust)
    check("the open payment is offered again", button(session, "Show the address") is not None)
    await dp.feed_update(bot, tap(cust, button(session, "Show the address")))
    check("…with the same address, and no second deposit", top["address"] in session.last_text() and len(api.topups) == 1)
    await dp.feed_update(bot, tap(other, Pay(a="show", id=p1.id).pack()))
    await dp.feed_update(bot, tap(other, Pay(a="chk", id=p1.id).pack()))
    check("another customer can neither see nor check it", top["address"] not in "".join(to(other))
          and (await repo.get_payment(p1.id)).status == Payment.PENDING)

    # ── it arrives: credited once, and the shop's wallet is funded ──
    api.pay_topup(int(p1.invoice_id))
    await asyncio.gather(payments.poll_all(), payments.poll_all(),
                         dp.feed_update(bot, tap(cust, Pay(a="chk", id=p1.id).pack())))
    async with repo.session_factory() as s:
        n_tx = await s.scalar(select(func.count(MemberTx.id)).where(MemberTx.member_id == mem.id,
                                                                     MemberTx.kind == "payment"))
    check("credited exactly once while the poller and the button race",
          (await repo.get_member(mem.id)).balance == D("10.00") and n_tx == 1
          and (await repo.get_payment(p1.id)).status == Payment.PAID)
    check("the shop's NumberHub wallet has the money to sell with", api.wallet == D("10.00"))
    check("the customer and the owner are told", any("Payment received" in x for x in to(cust))
          and any("NumberHub wallet" in x for x in to(OWNER)))
    await payments.poll_all()
    await dp.feed_update(bot, tap(cust, Pay(a="chk", id=p1.id).pack()))
    check("asking again adds nothing", (await repo.get_member(mem.id)).balance == D("10.00")
          and "already on your balance" in session.alerts()[-1])

    # ── fraud and mistakes ──
    p2 = await payments.open_link(r, mem.id, payments.NUMBERHUB, D("20"), "usdt_tron")
    api.pay_topup(int(p2.invoice_id), credited="12.50")
    await payments.poll_all()
    check("a short payment is credited as what arrived (12.50 of 20), never the asked amount",
          (await repo.get_member(mem.id)).balance == D("22.50") and (await repo.get_payment(p2.id)).credited == D("12.50")
          and api.wallet == D("22.50"))
    mem2 = await repo.get_or_create_member(r.id, other, None, None)
    pa = await payments.open_link(r, mem.id, payments.NUMBERHUB, D("5"), "usdt_bsc")
    pb = await payments.open_link(r, mem2.id, payments.NUMBERHUB, D("5"), "usdt_bsc")
    check("two customers asking for the same amount and network never share an address",
          pa.invoice_id != pb.invoice_id and payments.details(pa)["address"] != payments.details(pb)["address"])
    api.pay_topup(int(pb.invoice_id))
    await payments.poll_all()
    check("…and a payment is credited only to the customer it was opened for",
          (await repo.get_member(mem2.id)).balance == D("5.00") and (await repo.get_member(mem.id)).balance == D("22.50")
          and (await repo.get_payment(pa.id)).status == Payment.PENDING)
    mine = api.owner_topup("7.00")
    async with repo.session_factory() as s:
        await s.execute(update(Payment).where(Payment.id == pa.id).values(invoice_id=str(mine)))
        await s.commit()
    await payments.poll_all()
    check("a payment row pointed at another paid deposit (the owner's own top-up) is never credited",
          (await repo.get_payment(pa.id)).status == Payment.PENDING and (await repo.get_member(mem.id)).balance == D("22.50"))
    check("a built-in payment is never credited by its asked amount",
          await repo.credit_payment(pa.id) is None and (await repo.get_payment(pa.id)).status == Payment.PENDING)
    api.topup_supports_reference = False
    why = await refused(payments.open_link(r, mem.id, payments.NUMBERHUB, D("9"), "usdt_bsc"))
    async with repo.session_factory() as s:
        last = (await s.execute(select(Payment).where(Payment.member_id == mem.id).order_by(Payment.id.desc()))).scalars().first()
    check("an API that ignores the reference is refused: no address is shown and the row is closed",
          "per-customer" in why and last.status == Payment.FAILED and last.invoice_id is None, why)
    api.topup_supports_reference = True
    n = len(api.topups)
    api.lose_topup_replies = 1
    p5 = await payments.open_link(r, mem.id, payments.NUMBERHUB, D("11"), "usdt_bsc")
    check("a lost reply is asked again and still gives ONE deposit",
          len(api.topups) == n + 1 and p5.invoice_id == str(max(api.topups)) and payments.details(p5).get("address"))
    api.fail_next_topup = "too_many_open_topups"
    await to_amounts(cust)
    await dp.feed_update(bot, tap(cust, button(session, "$5.00")))
    await dp.feed_update(bot, tap(cust, button(session, "USDT")))
    await dp.feed_update(bot, tap(cust, button(session, "BEP20")))
    check("NumberHub refuses: the customer is told to try later, the owner is told why",
          "not available right now" in session.last_text()
          and any("could not get a payment address" in x and "too many customers" in x for x in to(OWNER)))
    await repo.update_reseller(r.id, status=Reseller.SUSPENDED)
    why = await refused(payments.open_link(await repo.get_reseller(r.id), mem.id, payments.NUMBERHUB, D("5"), "usdt_tron"))
    await repo.update_reseller(r.id, status=Reseller.ACTIVE)
    r = await repo.get_reseller(r.id)
    check("a shop that is not selling takes no money", "not selling" in why, why)
    before, payments.PER_DAY_PER_SHOP = payments.PER_DAY_PER_SHOP, await repo.payments_today_shop(r.id, payments.NUMBERHUB)
    why = await refused(payments.open_link(r, mem2.id, payments.NUMBERHUB, D("5"), "usdt_tron"))
    payments.PER_DAY_PER_SHOP = before
    check("the shop's daily ceiling stops new addresses", "too many payment addresses" in why, why)
    why = await refused(payments.open_link(r, mem2.id, payments.NUMBERHUB, D("5"), "xrp"))
    check("an unknown network is refused before anything is opened", "unknown network" in why, why)
    p6 = await payments.open_link(r, mem2.id, payments.NUMBERHUB, D("6"), "usdt_tron")
    api.topups[int(p6.invoice_id)]["status"] = "expired"
    await payments.poll_all()
    check("NumberHub says expired while a late confirmation is still possible: kept open, nothing added",
          (await repo.get_payment(p6.id)).status == Payment.PENDING and (await repo.get_member(mem2.id)).balance == D("5.00"))
    long_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=7)
    async with repo.session_factory() as s:
        await s.execute(update(Payment).where(Payment.id == p6.id).values(expires_at=long_ago))
        await s.commit()
    await payments.poll_all()
    check("…and given up once the grace time is over", (await repo.get_payment(p6.id)).status == Payment.EXPIRED)
    api.pay_topup(int(p6.invoice_id))            # confirmed late: the shop's wallet got it after all
    n_late = await payments.sweep_expired(force=True)
    await payments.sweep_expired(force=True)
    check("a payment that arrives after it was given up is still added, once",
          n_late == 1 and (await repo.get_payment(p6.id)).status == Payment.PAID
          and (await repo.get_member(mem2.id)).balance == D("11.00"), f"{n_late}")
    p8 = await payments.open_link(r, mem2.id, payments.NUMBERHUB, D("3"), "usdt_bsc")
    async with repo.session_factory() as s:
        await s.execute(update(Payment).where(Payment.id == p8.id).values(status=Payment.EXPIRED, expires_at=long_ago))
        await s.commit()
    api.pay_topup(int(p8.invoice_id))
    await dp.feed_update(bot, tap(other, Pay(a="chk", id=p8.id).pack()))
    check("…and the customer's own 'I've paid' finds it too",
          (await repo.get_payment(p8.id)).status == Payment.PAID and (await repo.get_member(mem2.id)).balance == D("14.00"))

    # ── another coin: Bitcoin has one network, so the address comes straight away ──
    mem4 = await repo.get_or_create_member(r.id, 9604, None, None)
    await to_amounts(9604)
    await dp.feed_update(bot, tap(9604, button(session, "$50.00")))
    check("for $50 Bitcoin is offered too", button(session, "BTC") is not None)
    await dp.feed_update(bot, tap(9604, button(session, "BTC")))
    pbtc = (await repo.open_payments(mem4.id))[0]
    tbtc = api.topups[int(pbtc.invoice_id)]
    text = session.last_text()
    check("Bitcoin: the address and the exact amount in BTC, no network question",
          "<code>0.00031</code> BTC on <b>Bitcoin</b>" in text and tbtc["address"] in text and tbtc["method"] == "btc"
          and payments.details(pbtc)["pay_currency"] == "BTC" and "Pay $50.00" in text, text[:260])
    check("…and a Bitcoin payment under its minimum is refused even if a button is forged",
          "least for this coin" in await refused(payments.open_link(r, mem4.id, payments.NUMBERHUB, D("5"), "btc")))
    api.pay_topup(int(pbtc.invoice_id))
    await payments.poll_all()
    check("…paid in full: the customer gets the 50.00 dollars, the owner is told it came in BTC",
          (await repo.get_member(mem4.id)).balance == D("50.00") and any("in BTC" in x for x in to(OWNER)))
    await to_amounts(9604)
    await dp.feed_update(bot, tap(9604, button(session, "$5.00")))
    await dp.feed_update(bot, tap(9604, button(session, "ETH")))
    check("a coin on two networks asks which", button(session, "Ethereum") is not None and button(session, "BEP20") is not None
          and len(await repo.open_payments(mem4.id)) == 0)
    check("the coin list comes from NumberHub once, then from memory for ten minutes",
          sum(1 for m_, p_ in api.calls if (m_, p_) == ("GET", "/topups/methods")) == 1)
    payments._methods_cache = None
    api.has_methods_list = False
    older = await payments.methods(r)
    api.has_methods_list = True
    payments._methods_cache = None
    check("an API from before the coin list: the two USDT networks, nothing else", sorted(older) == ["usdt_bsc", "usdt_tron"])

    # ── more arrives on a payment that was already credited ──
    before_more = (await repo.get_member(mem2.id)).balance
    top8 = api.topups[int(p8.invoice_id)]
    top8["credited"] += D("2.50")                 # a second transfer to the same address: NumberHub added it
    api.wallet += D("2.50")
    fresh8 = await repo.get_payment(p8.id)
    added = await payments.check_more(fresh8)
    again = await payments.check_more(await repo.get_payment(p8.id))
    await payments.sweep_expired(force=True)
    check("more money on a credited payment reaches the customer too, once",
          added == D("2.50") and again == 0 and (await repo.get_member(mem2.id)).balance == before_more + D("2.50")
          and (await repo.get_payment(p8.id)).credited == D("5.50"), f"{added} {again}")
    check("…the customer and the owner are told", any("+$2.50" in x or "$2.50" in x for x in to(other)[-2:])
          and any("More arrived" in x for x in to(OWNER)))
    check("…a stale amount is refused by the step that adds it",
          await repo.credit_payment_more(p8.id, D("3.00"), D("9.00")) is None)

    # ── a key from another NumberHub account while a customer's payment is recent ──
    from app.bots import builder as _builder
    keep_nh = _builder.NumberHub
    elsewhere = FakeNumberHub(key="nh_live_another_account_0123456789")
    recent = await repo.recent_builtin_topup(r.id)
    _builder.NumberHub = lambda key: elsewhere.client(key)
    _bal, why_key = await _builder._check_key(OWNER, elsewhere.key, None, recent)
    _builder.NumberHub = lambda key: api.client(key)
    bal_ok, why_ok = await _builder._check_key(OWNER, api.key, None, recent)
    _builder.NumberHub = keep_nh
    check("a key from another NumberHub account is refused while a customer's payment is recent",
          recent is not None and _bal is None and "different NumberHub account" in (why_key or ""), str(why_key))
    check("…a key from the same account is accepted", bal_ok is not None and why_ok is None, str(why_ok))

    # ── taps sent together, quick re-asks, and a wallet that was spent ──
    mem3 = await repo.get_or_create_member(r.id, 9603, None, None)
    await dp.feed_update(bot, msg(9603, "/start"))
    await asyncio.gather(*[dp.feed_update(bot, tap(9603, Pay(a="via", g="nb", c=500 + i).pack())) for i in range(6)])
    n_open = len(await repo.open_payments(mem3.id))
    check("six taps at once from one customer: three addresses, not six", n_open == payments.OPEN_PER_MEMBER, str(n_open))
    pc = (await repo.open_payments(mem3.id))[0]
    asks = lambda: sum(1 for m_, p_ in api.calls if m_ == "GET" and p_ == f"/topups/{pc.invoice_id}")  # noqa: E731
    payments.ASK_EVERY_FRESH = 20.0
    payments._last_ask.clear()
    n0 = asks()
    for _ in range(5):
        await dp.feed_update(bot, tap(9603, Pay(a="chk", id=pc.id).pack()))
    payments.ASK_EVERY_FRESH = 0.0
    check("five quick 'I've paid' taps ask NumberHub once", asks() - n0 == 1, str(asks() - n0))
    kept, api.wallet = api.wallet, D("1.00")
    why = await refused(payments.open_link(r, mem2.id, payments.NUMBERHUB, D("5"), "usdt_tron"))
    api.wallet = kept
    check("a wallet that no longer covers what customers paid in: no new payment addresses, and the reason is the owner's to read",
          "no longer covers" in why, why)
    cover = await payments.backing(r)
    check("…the cover is the wallet against what customers paid in and still hold",
          cover is not None and cover[0] == api.wallet and cover[1] == D("89.00"), str(cover))
    check("…and payments work again once the wallet covers it",
          not await refused(payments.open_link(r, mem2.id, payments.NUMBERHUB, D("5"), "usdt_tron")))

    # ── the owner turns it off: an address already given still counts ──
    p7 = await payments.open_link(r, mem2.id, payments.NUMBERHUB, D("4"), "usdt_bsc")
    await dp.feed_update(bot, msg(OWNER, "/admin"))
    await dp.feed_update(bot, tap(OWNER, button(session, "Payments")))
    await dp.feed_update(bot, tap(OWNER, button(session, "Turn off built-in crypto")))
    r = await repo.get_reseller(r.id)
    check("turned off: no new addresses", r.nh_pay == 0 and payments.connected(r) == []
          and "off" in await refused(payments.open_link(r, mem2.id, payments.NUMBERHUB, D("5"), "usdt_bsc")))
    api.pay_topup(int(p7.invoice_id))
    await payments.poll_all()
    check("…but money sent to an address given before is still added",
          (await repo.get_member(mem2.id)).balance == D("20.50") and (await repo.get_payment(p7.id)).status == Payment.PAID)

    # ── the platform's default for shops whose owner has not chosen ──
    api2 = FakeNumberHub()
    r2 = await make_reseller(api2, bot_id=86100)
    settings.numberhub_pay_default = True
    try:
        on_by_default = payments.builtin_on(r2) and payments.connected(r2) == [payments.NUMBERHUB]
        await payments.set_builtin(r2.id, False)
        owner_said_no = not payments.builtin_on(await repo.get_reseller(r2.id))
    finally:
        settings.numberhub_pay_default = False
    check("the platform default applies only while the owner has not chosen", on_by_default and owner_said_no
          and not payments.builtin_on(await repo.get_reseller(r2.id)))
    payments.ASK_EVERY_FRESH, payments.ASK_EVERY_OLD, payments.ASK_EVERY_LATE = 20.0, 60.0, 3600.0
    payments._methods_cache = None
    payments.TRANSPORT = None
    await forget_paid_in()


async def test_owner_stats():
    """Owner 2026-11-23 (#122): statistics in the bot for the people who run it. The shop
    owner gets the last 30 days on one screen, why purchases did not go through, and a
    report every morning, only on days the shop was used."""
    print("statistics for the shop owner, and the morning report")
    import datetime as dtm
    from aiogram.methods import SendMessage
    from app import reports
    from app.bots.reseller import build_router
    from sqlalchemy import select
    api = FakeNumberHub(wallet="50.00")
    r = await make_reseller(api, bot_id=86201)
    session = FakeSession()
    bot = fake_bot(session)
    runtime._bots[r.id] = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(r.id))
    to = lambda uid: [m.text or "" for m in session.requests if isinstance(m, SendMessage) and m.chat_id == uid]  # noqa: E731
    a = await member(r, 9701, "5")
    b = await member(r, 9702, "5")

    # two codes, one cancelled, and two purchases NumberHub refuses
    o1 = await selling.buy(r, a, "wa", "187", None)
    api.set_status(o1.nh_id, "received", "111222")
    o2 = await selling.buy(r, b, "wa", "187", None)
    api.set_status(o2.nh_id, "received", "333444")
    o3 = await selling.buy(r, a, "tg", "187", None)
    api.set_status(o3.nh_id, "canceled")
    await selling.sync_reseller(r)
    await selling.settle_sweep()
    for _ in range(2):
        api.fail_next_buy = "insufficient_funds"
        try:
            await selling.buy(r, await repo.get_member(b.id), "wa", "6", None)
        except SellError:
            pass
    async with repo.session_factory() as s:
        rows = list((await s.execute(select(Order).where(Order.reseller_id == r.id, Order.status == Order.FAILED))).scalars())
    check("a purchase NumberHub refuses keeps NumberHub's reason", len(rows) == 2
          and all(o.fail_reason == "insufficient_funds" for o in rows), str([o.fail_reason for o in rows]))
    pay = await repo.create_payment(r.id, a.id, "numberhub", D("10"))
    await repo.attach_invoice(pay.id, "9001", None, dtm.datetime.now(dtm.timezone.utc) + dtm.timedelta(hours=1), details="{}")
    await repo.credit_payment(pay.id, D("10"))
    lost = await repo.create_payment(r.id, b.id, "numberhub", D("5"))
    await repo.attach_invoice(lost.id, "9002", None, dtm.datetime.now(dtm.timezone.utc) + dtm.timedelta(hours=1), details="{}")

    rep = await repo.shop_report(r.id, 30)
    check("the figures: orders by outcome, codes, sales and profit",
          rep["orders"] == 5 and rep["codes"] == 2 and rep["outcomes"].get("failed") == 2
          and rep["outcomes"].get("canceled") == 1 and rep["sales"] == D("0.78") and rep["profit"] == D("0.18"), str(rep)[:300])
    check("…why, best apps, customers and payments",
          rep["reasons"] == [("insufficient_funds", 2)] and rep["apps"][0][:3] == ("WhatsApp", 2, 2)
          and rep["new_members"] == 2 and rep["buyers"] == 2
          and rep["payments"] == [("numberhub", 2, 1, D("10.00"))], f"{rep['reasons']} {rep['apps']} {rep['payments']}")

    await dp.feed_update(bot, msg(OWNER, "/admin"))
    check("the admin panel has 📊 Statistics", button(session, "Statistics") is not None)
    await dp.feed_update(bot, tap(OWNER, button(session, "Statistics")))
    text = session.last_text()
    check("the screen: sales, profit, numbers given against codes delivered",
          "Sold <b>$0.78</b>" in text and "profit ≈ <b>$0.18</b>" in text
          and "Numbers given: <b>3</b> · codes delivered: <b>2</b> (67%)" in text, text[:300])
    check("…why purchases did not go through, in words the owner can act on",
          "your NumberHub wallet was empty ×2" in text and "purchase did not go through: 2" in text)
    check("…best apps with their delivery rate, customers, payments",
          "WhatsApp: 2 of 2 (100%) · $0.78 · $0.18" in text and "Telegram: 0 of 1 (0%)" in text and "bought: 2" in text
          and "Built-in crypto: 1 of 2 (50%) · $10.00" in text, text[300:900])
    check("…the morning report is on, with its switch", "Morning report: <b>on</b>" in text
          and button(session, "Morning report off") is not None)

    now = dtm.datetime.now(dtm.timezone.utc)
    early, morning = now.replace(hour=2, minute=0), now.replace(hour=6, minute=0)
    n_before = len(to(OWNER))
    check("no report before the morning hour", await reports.daily_once(early) == 0 and len(to(OWNER)) == n_before)
    sent = await reports.daily_once(morning)
    msgs = to(OWNER)[n_before:]
    check("then one message to the owner: the last 24 hours of their shop", sent >= 1 and len(msgs) == 1
          and "the last 24 hours" in msgs[0] and "Sold <b>$0.78</b>" in msgs[0] and "2 codes" in msgs[0]
          and "your NumberHub wallet was empty ×2" in msgs[0] and "Payments received: 1 · $10.00" in msgs[0], str(msgs)[:400])
    await reports.daily_once(morning)
    check("not again the same day", len(to(OWNER)) == n_before + 1)
    await dp.feed_update(bot, msg(OWNER, "/admin"))
    await dp.feed_update(bot, tap(OWNER, button(session, "Statistics")))
    await dp.feed_update(bot, tap(OWNER, button(session, "Morning report off")))
    fresh = await repo.get_reseller(r.id)
    check("the owner switches it off with one tap", fresh.daily_report == 0 and "Morning report: <b>off</b>" in session.last_text()
          and button(session, "Morning report on") is not None)
    n_off = len(to(OWNER))
    await reports.daily_once(morning + dtm.timedelta(days=1))
    check("…and gets none the next day", len(to(OWNER)) == n_off)

    quiet_api = FakeNumberHub()
    quiet = await make_reseller(quiet_api, bot_id=86202)
    qs = FakeSession()
    runtime._bots[quiet.id] = fake_bot(qs)
    await reports.daily_once(morning + dtm.timedelta(days=2))
    check("a shop nobody used gets no report", not [m for m in qs.requests if isinstance(m, SendMessage)]
          and await reports.daily_text(quiet) is None)
    idle = await reports.stats_text(quiet, 30)
    check("…and its statistics screen opens with zeros", "Sold <b>$0.00</b>" in idle and "(–)" in idle)


async def test_kasera_qris():
    """A shop owner in Indonesia asked for it (support chat, 2026-11-23): customers pay
    by QRIS through the owner's own KaseraPay account and are credited by themselves."""
    print("QRIS through the owner's own KaseraPay account")
    from aiogram.methods import SendMessage, SendPhoto
    from app import payments
    from app.bots.callbacks import Pay
    from app.bots.reseller import build_router
    from app.models import Payment
    from fakes import FakeFx, FakeGateways
    from app import fx
    gw = FakeGateways()
    payments.TRANSPORT = gw.transport()
    fakefx = FakeFx()
    fx.TRANSPORT, fx._rate = fakefx.transport(), None
    await fx.refresh()
    api = FakeNumberHub()
    r = await make_reseller(api, bot_id=86301)
    session = FakeSession()
    bot = fake_bot(session)
    runtime._bots[r.id] = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(r.id))
    cust = 9801
    to = lambda uid: [m.text or "" for m in session.requests if isinstance(m, SendMessage) and m.chat_id == uid]  # noqa: E731
    photos = lambda uid: [m for m in session.requests if isinstance(m, SendPhoto) and m.chat_id == uid]        # noqa: E731

    async def refused(coro) -> str:
        try:
            await coro
        except payments.PayError as exc:
            return str(exc) or "refused"
        return ""

    async def add(uid, amount_button):
        await dp.feed_update(bot, msg(uid, "/start"))
        await dp.feed_update(bot, tap(uid, button(session, "Balance")))
        await dp.feed_update(bot, tap(uid, button(session, "Add balance")))
        await dp.feed_update(bot, tap(uid, button(session, amount_button)))

    # ── the owner connects it ──
    await dp.feed_update(bot, msg(OWNER, "/admin"))
    await dp.feed_update(bot, tap(OWNER, button(session, "Payments")))
    check("💳 Payments offers KaseraPay next to the other two", button(session, "Connect 🇮🇩 KaseraPay") is not None
          and "KaseraPay (QRIS): <b>— not connected</b>" in session.last_text())
    await dp.feed_update(bot, tap(OWNER, button(session, "Connect 🇮🇩 KaseraPay")))
    check("…with how to get the key and what a test key does", "pay.kasera.id" in session.last_text()
          and "kp_test_" in session.last_text())
    await dp.feed_update(bot, msg(OWNER, "kp_live_WRONGKEY00000000000"))
    check("a wrong key is refused", "didn't accept" in session.last_text()
          and (await repo.get_reseller(r.id)).kasera_key_enc is None)
    await dp.feed_update(bot, msg(OWNER, gw.ks_key))
    fresh = await repo.get_reseller(r.id)
    check("the right key is checked without making a payment and stored encrypted",
          fresh.kasera_key_enc and gw.ks_key not in fresh.kasera_key_enc and gw.ks == {})
    check("no rate is asked: dollars turn into rupiah at today's rate, and QRIS is on at once",
          fresh.kasera_auto == 1 and fresh.kasera_rate is None and payments.connected(fresh) == [payments.KASERA]
          and payments.kasera_rate(fresh) == D("18196") and "How many rupiah" not in session.last_text()
          and "connected" in session.last_text() and "Rp 18.196 = $1</b> (today's rate)" in session.last_text()
          and "pays <b>Rp 90.980</b>" in session.last_text() and "Today a dollar costs <b>Rp 18.195</b>" in session.last_text(),
          session.last_text()[:260])
    await add(9803, "$5.00")
    check("a customer adding $5.00 gets a QR for today's rupiah (Rp 18.196 for each dollar)",
          len(photos(9803)) == 1 and "Rp 90.980" in photos(9803)[0].caption and "$5.00" in photos(9803)[0].caption
          and gw.ks_bodies[-1]["amount"] == 90980, str(photos(9803)[0].caption if photos(9803) else None)[:120])

    async def rate_screen():
        await dp.feed_update(bot, msg(OWNER, "/admin"))
        await dp.feed_update(bot, tap(OWNER, button(session, "Payments")))
        await dp.feed_update(bot, tap(OWNER, button(session, "Rupiah for $1")))

    async def own_rate(value):
        await rate_screen()
        await dp.feed_update(bot, tap(OWNER, button(session, "Set my own rate")))
        await dp.feed_update(bot, msg(OWNER, value))

    await rate_screen()
    check("the rate screen offers a percent on top and an own rate, and not what is already on",
          button(session, "Percent on top") is not None and button(session, "Set my own rate") is not None
          and button(session, "Use today's rate") is None)
    await dp.feed_update(bot, tap(OWNER, button(session, "Percent on top")))
    await dp.feed_update(bot, msg(OWNER, "many"))
    check("a percent that is not a number is asked again", "from 0 to 20" in session.last_text())
    await dp.feed_update(bot, msg(OWNER, "25"))
    check("…and so is one over 20", "from 0 to 20" in session.last_text()
          and (await repo.get_reseller(r.id)).kasera_extra is None)
    await dp.feed_update(bot, msg(OWNER, "3"))
    fresh = await repo.get_reseller(r.id)
    check("3% on top of today's rate: Rp 18.742 for $1",
          fresh.kasera_extra == D("3") and payments.kasera_rate(fresh) == D("18742")
          and "today's rate + 3%" in session.last_text() and "Rp 18.742 = $1" in session.last_text(), session.last_text()[:200])
    await dp.feed_update(bot, tap(OWNER, button(session, "Percent on top")))
    await dp.feed_update(bot, msg(OWNER, "0"))
    check("back to the plain rate of the day", payments.kasera_rate(await repo.get_reseller(r.id)) == D("18196"))

    # ── where today's rate comes from ──
    check("the rate was read from the first source and kept in the database",
          fx.current() == D("18195.480115") and fakefx.calls == ["open.er-api.com"])
    fakefx.down = {"open.er-api.com"}
    await fx.refresh()
    check("the first source is down: the second one is asked", fx.current() == D("18190.1")
          and fakefx.calls[-2:] == ["open.er-api.com", "cdn.jsdelivr.net"])
    fakefx.down = {"open.er-api.com", "cdn.jsdelivr.net"}
    check("both are down: the rate read before stays", await fx.refresh() == D("18190.1") and fx.current() == D("18190.1"))
    fx._rate = None
    check("after a restart the kept rate is there before anything is read", await fx.load() == D("18190.1"))
    fakefx.down, fakefx.first = set(), 5.25
    await fx.refresh()
    check("an answer that cannot be a rupiah rate is not believed: the next source is used",
          fx.current() == D("18190.1") and fakefx.calls[-2:] == ["open.er-api.com", "cdn.jsdelivr.net"])
    fakefx.first = 18195.480115
    kept = fx._rate
    fx._rate = (kept[0] - fx.MAX_AGE_SEC - 60, kept[1])
    fresh = await repo.get_reseller(r.id)
    check("a rate older than four days is not used: no QRIS is offered until a new one is read",
          fx.current() is None and payments.kasera_rate(fresh) is None and payments.connected(fresh) == [])
    await dp.feed_update(bot, msg(OWNER, "/admin"))
    await dp.feed_update(bot, tap(OWNER, button(session, "Payments")))
    check("…and the owner's Payments screen says so", "could not be read yet" in session.last_text())
    await fx.refresh()
    check("read again: QRIS is back", payments.connected(await repo.get_reseller(r.id)) == [payments.KASERA])

    # ── the owner's own rate ──
    await own_rate("abc")
    check("a rate that is not a number is asked again", "from 1000 to 100000" in session.last_text())
    await dp.feed_update(bot, msg(OWNER, "16.500"))
    r = await repo.get_reseller(r.id)
    check("an own rate is saved and used instead of today's: from $0.31 to $60.60 at a time",
          r.kasera_rate == D("16500") and r.kasera_auto == 0 and payments.connected(r) == [payments.KASERA]
          and payments.kasera_rate(r) == D("16500") and payments.kasera_limits(r) == (D("0.31"), D("60.60"))
          and "your own rate: Rp 16.500 = $1" in session.last_text(), session.last_text()[:260])
    check("a usual rate gets no warning", "Check this rate" not in session.last_text())
    await own_rate("5000")
    check("a rate far under what a dollar costs is saved, and the owner is told what it means in their numbers",
          (await repo.get_reseller(r.id)).kasera_rate == D("5000") and "Check this rate" in session.last_text()
          and "pays Rp 5.000 gets $1.00" in session.last_text(), session.last_text()[:200])
    await rate_screen()
    check("…the rate screen says so too, next to what a dollar costs today, and offers today's rate",
          "Check this rate" in session.last_text() and "Today a dollar costs" in session.last_text()
          and button(session, "Use today's rate") is not None)
    await dp.feed_update(bot, tap(OWNER, button(session, "Use today's rate")))
    fresh = await repo.get_reseller(r.id)
    check("one tap puts the shop on today's rate; the typed rate is kept but not used",
          fresh.kasera_auto == 1 and fresh.kasera_rate == D("5000") and payments.kasera_rate(fresh) == D("18196")
          and "Check this rate" not in session.last_text())
    await own_rate("30000")
    check("a rate far over it is told the other way round",
          "pays Rp 30.000 for $1.00" in session.last_text(), session.last_text()[:200])
    await own_rate("16500")
    r = await repo.get_reseller(r.id)
    check("back to a usual rate: saved, no warning",
          r.kasera_rate == D("16500") and r.kasera_auto == 0 and "Check this rate" not in session.last_text())

    # ── manual deposits go away with one tap ──
    await repo.update_reseller(r.id, deposit_info="Pay me at the bank, then send the receipt")
    await dp.feed_update(bot, msg(OWNER, "/admin"))
    await dp.feed_update(bot, tap(OWNER, button(session, "Payments")))
    await dp.feed_update(bot, tap(OWNER, button(session, "Turn off manual deposits")))
    r = await repo.get_reseller(r.id)
    check("manual deposits are switched off with one tap, and the button goes",
          r.deposit_info is None and "Manual deposits are off" in session.last_text()
          and button(session, "Turn off manual deposits") is None)

    # ── a customer adds $5 ──
    await add(cust, "$5.00")
    mem = await repo.get_or_create_member(r.id, cust, None, None)
    p1 = (await repo.open_payments(mem.id))[0]
    shot = photos(cust)
    d1 = payments.details(p1)
    check("a QR in the chat with the rupiah to pay and the dollars it adds",
          len(shot) == 1 and "Rp 82.500" in shot[0].caption and "$5.00" in shot[0].caption
          and re.search(r"within (59|60) minutes", shot[0].caption) and d1["idr"] == 82500 and d1["qr"].startswith("000201"),
          str(shot[0].caption if shot else None)[:200])
    check("the gateway was asked for exactly that: rupiah, QRIS only, one payment per request",
          gw.ks_bodies[-1]["amount"] == 82500 and gw.ks_bodies[-1]["payment_methods"] == ["qris"]
          and gw.ks_bodies[-1]["merchant_ref"] == f"s{r.id}-p{p1.id}" and list(gw.ks_idem)[-1] == f"s{r.id}-p{p1.id}"
          and len(gw.ks_idem) == len(gw.ks_bodies))
    check("the picture is a real PNG", payments.qr_png(d1["qr"])[:8] == b"\x89PNG\r\n\x1a\n")
    left = (p1.expires_at.replace(tzinfo=dt.timezone.utc) - dt.datetime.now(dt.timezone.utc)).total_seconds() / 60
    check("the gateway's Jakarta time is stored as UTC: open for 60 minutes, not for 8 hours", 58 < left <= 60.1,
          f"{left:.1f} minutes")
    gw.ks_naive = True
    _, _, until = await payments.Kasera(gw.ks_key).create_qris(20000, "naive-1", "a time without a zone")
    gw.ks_naive = False
    left = (until - dt.datetime.now(dt.timezone.utc)).total_seconds() / 60
    check("a gateway time without a zone can never keep a payment open longer than was asked for",
          until.tzinfo is not None and 58 < left <= 60.1, f"{left:.1f} minutes")
    await dp.feed_update(bot, msg(cust, "/start"))
    await dp.feed_update(bot, tap(cust, button(session, "Balance")))
    await dp.feed_update(bot, tap(cust, button(session, "Add balance")))
    check("an open QRIS payment is offered as its QR, not as an address",
          button(session, "Show the QR") is not None and "see its QR again" in session.last_text()
          and "address" not in session.last_text(), session.last_text()[:200])
    await dp.feed_update(bot, tap(cust, button(session, "Show the QR")))
    check("…and one tap sends the QR again", len(photos(cust)) == 2 and "Rp 82.500" in photos(cust)[-1].caption)
    await dp.feed_update(bot, tap(cust, Pay(a="chk", id=p1.id).pack()))
    check("'I've paid' before it arrives: not yet", "Not received yet" in session.alerts()[-1]
          and (await repo.get_member(mem.id)).balance == 0)
    gw.ks[p1.invoice_id]["status"] = "succeeded"
    await asyncio.gather(payments.poll_all(), payments.poll_all(),
                         dp.feed_update(bot, tap(cust, Pay(a="chk", id=p1.id).pack())))
    check("paid: $5.00 added exactly once, both are told",
          (await repo.get_member(mem.id)).balance == D("5.00") and (await repo.get_payment(p1.id)).status == Payment.PAID
          and any("Payment received" in x for x in to(cust)) and any("KaseraPay (QRIS)" in x for x in to(OWNER)))

    # ── what must not credit ──
    p2 = await payments.open_link(r, mem.id, payments.KASERA, D("10"))
    gw.ks[p2.invoice_id].update(status="succeeded", amount=1000)
    await payments.poll_all()
    check("a payment the gateway reports with another amount is not credited",
          (await repo.get_payment(p2.id)).status == Payment.PENDING and (await repo.get_member(mem.id)).balance == D("5.00"))
    gw.ks[p2.invoice_id].update(status="expired", amount=165000)
    await payments.poll_all()
    check("expired at the gateway inside the grace time: kept open (it can still turn paid)",
          (await repo.get_payment(p2.id)).status == Payment.PENDING)
    gw.ks[p2.invoice_id]["status"] = "succeeded"
    await payments.poll_all()
    check("…and credited when it does", (await repo.get_member(mem.id)).balance == D("15.00"))
    check("too small or too large for one QRIS payment is refused before the gateway is asked",
          "from $0.31 to $60.60" in await refused(payments.open_link(r, mem.id, payments.KASERA, D("100")))
          and "from $0.31" in await refused(payments.open_link(r, mem.id, payments.KASERA, D("0.05"))))
    gw.refuse_create = True
    n_owner = len(to(OWNER))
    await add(cust, "$10.00")
    gw.refuse_create = False
    check("the gateway refuses (an unverified account's limit): the customer is told, the owner is told why",
          "not available right now" in session.last_text()
          and any("Unverified accounts" in x for x in to(OWNER)[n_owner:]), str(to(OWNER)[n_owner:])[:200])

    # ── a test key: the flow works, no balance is added ──
    await payments.connect(r.id, payments.KASERA, gw.ks_test_key)
    r = await repo.get_reseller(r.id)
    p3 = await payments.open_link(r, mem.id, payments.KASERA, D("7"))
    gw.ks[p3.invoice_id]["status"] = "succeeded"
    n_owner = len(to(OWNER))
    await payments.poll_all()
    check("a test key's payment is reported as working and adds nothing",
          payments.details(p3)["test"] is True and (await repo.get_member(mem.id)).balance == D("15.00")
          and (await repo.get_payment(p3.id)).status == Payment.EXPIRED
          and any("Test payment worked" in x for x in to(OWNER)[n_owner:]))

    # ── with other ways to pay, QRIS is one of the choices ──
    await payments.connect(r.id, payments.KASERA, gw.ks_key)
    await payments.set_builtin(r.id, True)
    r = await repo.get_reseller(r.id)
    payments._methods_cache = None
    await add(9802, "$5.00")
    check("QRIS is offered next to the coins", button(session, "QRIS") is not None and button(session, "USDT") is not None)
    await add(9802, "$50.00")
    await dp.feed_update(bot, tap(9802, button(session, "QRIS")))
    check("…and tapping it sends the QR", len(photos(9802)) == 1 and "Rp 825.000" in photos(9802)[0].caption)
    await repo.update_reseller(r.id, kasera_rate=D("25000"))
    r = await repo.get_reseller(r.id)
    await add(9802, "$5.00")                     # to the amount screen, then the $50 preset itself
    await dp.feed_update(bot, tap(9802, Pay(a="amt", c=5000).pack()))
    shown = [b_.text for row in (session.last_markup().inline_keyboard if session.last_markup() else []) for b_ in row]
    check("…but not for an amount over Rp 1.000.000 at the shop's rate", button(session, "QRIS") is None
          and button(session, "USDT") is not None, f"{shown} | {session.last_text()[:120]}")

    # ── the owner removes it ──
    await dp.feed_update(bot, msg(OWNER, "/admin"))
    await dp.feed_update(bot, tap(OWNER, button(session, "Payments")))
    await dp.feed_update(bot, tap(OWNER, button(session, "Remove KaseraPay")))
    gone = await repo.get_reseller(r.id)
    check("removed: the key is gone and QRIS is no longer offered", gone.kasera_key_enc is None
          and payments.KASERA not in payments.connected(gone))
    payments._methods_cache = None
    payments.TRANSPORT = None
    fx.TRANSPORT = None


async def test_paid_left():
    """What a customer paid in is theirs: the owner cannot take it away, and a balance
    added by hand cannot buy with what OTHER customers paid in."""
    print("paid-in money and balances added by hand")
    from app import payments
    from app.config import settings
    own_a, own_b = 5127, 5128
    api = FakeNumberHub(wallet="0.00")
    r = await make_reseller(api, owner=own_a, bot_id=86401)
    session = FakeSession(bot_id=86401)
    runtime._bots[r.id] = fake_bot(session)
    told = lambda words: [x for x in session.texts() if words in x]      # noqa: E731

    async def pay_in(m, amount, deposit):
        """A built-in payment of this customer arrives: in the wallet and on their balance."""
        row = await repo.create_payment(r.id, m.id, "numberhub", D(amount))
        await repo.attach_invoice(row.id, deposit, None, None)
        await repo.credit_payment(row.id, D(amount))
        api.wallet += D(amount)
        return row

    async def try_buy(m):
        try:
            return await selling.buy(r, await repo.get_member(m.id), "wa", "187", None)
        except SellError as e:
            return e.reason

    payer = await member(r, 91001)
    await pay_in(payer, "10", "9401")
    p = await repo.get_member(payer.id)
    check("a built-in payment is the customer's own money: all of the balance is paid in",
          p.balance == D("10") and p.paid_left == D("10") and p.by_hand == 0
          and await repo.builtin_unspent(r.id) == D("10.00") and await repo.owner_paid_left(own_a) == D("10.00"))
    await repo.member_adjust(r.id, payer.id, D("2"))
    p = await repo.get_member(payer.id)
    check("a bonus the owner adds is not: $12 balance, $10 paid in, $2 by hand",
          p.balance == D("12") and p.paid_left == D("10") and p.by_hand == D("2"))
    from app.bots.callbacks import Adm
    from app.bots.reseller import build_router
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(r.id))
    bot = runtime._bots[r.id]
    await dp.feed_update(bot, msg(own_a, "/admin"))
    await dp.feed_update(bot, tap(own_a, Adm(a="remove").pack()))
    await dp.feed_update(bot, msg(own_a, f"{payer.telegram_id} 5"))
    check("the owner asking to remove $5 is told what can be removed and why, and nothing is taken",
          "at most <b>$2.00</b>" in session.last_text() and "<b>$10.00</b> of their balance" in session.last_text()
          and "paid in themselves" in session.last_text() and (await repo.get_member(payer.id)).balance == D("12"),
          session.last_text()[:200])
    await dp.feed_update(bot, msg(own_a, "-"))
    check("the owner can take back only what they added by hand",
          not await repo.member_adjust(r.id, payer.id, D("-3")) and await repo.member_adjust(r.id, payer.id, D("-2"))
          and not await repo.member_adjust(r.id, payer.id, D("-0.01")) and (await repo.get_member(payer.id)).balance == D("10"))

    hand1 = await member(r, 91002, "5")
    got = await try_buy(hand1)
    await asyncio.sleep(0.05)
    h = await repo.get_member(hand1.id)
    check("a customer with a hand-added balance cannot buy while the wallet holds only other customers' money",
          got == "paused" and h.held == 0 and not api.orders, f"{got}")
    check("…and the owner is told why, in their own bot", len(told("added by hand")) == 1, str(session.texts()[-1:]))
    o1 = await try_buy(payer)
    check("the customer who paid buys as always", isinstance(o1, Order) and api.held == D("0.30"), str(o1))
    api.owner_topup("1")                                           # the owner puts $1 of their own in the wallet
    o2 = await try_buy(hand1)
    hand2, hand3 = await member(r, 91003, "5"), await member(r, 91004, "5")
    o3, o4 = await try_buy(hand2), await try_buy(hand3)
    check("with $1 of the owner's own money behind them, hand-added balances buy until it is used up",
          isinstance(o2, Order) and isinstance(o3, Order) and o4 == "paused" and api.held == D("0.90"),
          f"{o2} {o3} {o4} held {api.held}")

    api.set_status(o1.nh_id, "received", "111222")
    await selling.sync_reseller(r)
    p = await repo.get_member(payer.id)
    check("a code arrives for the paying customer: charged from their paid-in money",
          p.balance == D("9.61") and p.paid_left == D("9.61") and await repo.builtin_unspent(r.id) == D("9.61"))
    await repo.member_adjust(r.id, payer.id, D("3"))
    p = await repo.get_member(payer.id)
    check("paid-in money is spent first: with $9.61 paid in and $3 by hand, a $0.39 number is all paid-in money",
          selling.by_hand_part(p, D("0.39")) == 0 and selling.by_hand_part(p, D("10.00")) == D("0.39"))

    mixed = await member(r, 91005, "1")
    await pay_in(mixed, "0.20", "9402")
    x = await repo.get_member(mixed.id)
    check("a customer with $0.20 paid in and $1 by hand: $0.19 of a $0.39 number is by hand",
          x.balance == D("1.20") and x.paid_left == D("0.20") and selling.by_hand_part(x, D("0.39")) == D("0.19"))
    api.owner_topup("0.50")
    o5 = await try_buy(mixed)
    api.set_status(o5.nh_id, "received", "333444")
    await selling.sync_reseller(r)
    x = await repo.get_member(mixed.id)
    check("…bought, charged: the paid-in part is gone first, what is left was added by hand",
          isinstance(o5, Order) and x.balance == D("0.81") and x.paid_left == 0 and x.by_hand == D("0.81"))

    # several at once: the owner's own money covers exactly one more number
    for o in (o2, o3):
        api.set_status(o.nh_id, "expired")
    await selling.sync_reseller(r)
    owed = await repo.owner_paid_left(own_a)
    api.wallet = owed + api.held + D("0.30")
    crowd = [await member(r, 91010 + i, "5") for i in range(6)]
    got = await asyncio.gather(*(try_buy(m) for m in crowd))
    check("six hand-added balances buying at once with the owner's money for one: exactly one buys",
          sum(isinstance(g, Order) for g in got) == 1 and sum(g == "paused" for g in got) == 5,
          str([g if isinstance(g, str) else "bought" for g in got]))
    waiting = next(m for m, g in zip(crowd, got) if g == "paused")      # one who has no order open
    orders_before = len(api.orders)
    api.scopes.discard("wallet:read")
    got = await try_buy(waiting)
    api.scopes.add("wallet:read")
    check("the wallet cannot be read: nothing is bought, and nothing is blamed on the owner",
          got == "busy" and (await repo.get_member(waiting.id)).held == 0 and len(api.orders) == orders_before,
          str(got))

    # a shop whose customers paid nothing in: nothing changes
    api_b = FakeNumberHub(wallet="0.10")
    rb = await make_reseller(api_b, owner=own_b, bot_id=86402)
    session_b = FakeSession(bot_id=86402)
    runtime._bots[rb.id] = fake_bot(session_b)
    mb = await member(rb, 91020, "5")
    try:
        await selling.buy(rb, mb, "wa", "187", None)
        got = "bought"
    except SellError as e:
        got = e.reason
    await asyncio.sleep(0.05)
    check("no paid-in money anywhere in the owner's shops: the wallet is not even asked, NumberHub decides as before",
          got == "paused" and ("GET", "/balance") not in api_b.calls
          and any("balance is too low" in x for x in session_b.texts()), f"{got} {api_b.calls[-3:]}")

    # the figures the wallet service reads are exact now
    figures = {f["shop_id"]: f for f in await repo.builtin_figures()}
    check("what the shop owes is what its customers have left of their paid-in money, to the cent",
          figures[r.id]["customers_hold"] == str(await repo.builtin_unspent(r.id)) == "9.61"
          and rb.id not in figures, str(figures.get(r.id)))

    # a shop on a key its owner pasted, on a platform run for others
    keep = settings.provision_secret
    try:
        await repo.update_reseller(r.id, key_by_owner=1, nh_pay=1)
        pasted = await repo.get_reseller(r.id)
        settings.provision_secret = ""
        check("running the platform for yourself: built-in payments work on your own key", payments.builtin_on(pasted))
        settings.provision_secret = "s" * 40
        check("on a platform run for others, a shop on a key its owner pasted offers no built-in payments",
              not payments.builtin_allowed(pasted) and not payments.builtin_on(pasted)
              and payments.NUMBERHUB not in payments.connected(pasted))
        refused = ""
        try:
            await payments.open_link(pasted, payer.id, payments.NUMBERHUB, D("5"), "usdt_bsc")
        except payments.PayError as exc:
            refused = str(exc)
        check("…opening one by hand is refused", "off" in refused, refused)
        from app.bots.admin import deposits_screen
        text, markup = await deposits_screen(pasted)
        labels = [b.text for row in markup.inline_keyboard for b in row]
        check("…and the owner's Payments screen says why, with no switch",
              "pasted yourself" in text and not any("built-in crypto" in x for x in labels), str(labels))
        await repo.update_reseller(r.id, key_by_owner=0)
        handed = await repo.get_reseller(r.id)
        check("the same shop on a key the wallet service handed over: built-in payments as chosen",
              payments.builtin_allowed(handed) and payments.builtin_on(handed))
    finally:
        settings.provision_secret = keep
    runtime._bots.pop(r.id, None)
    runtime._bots.pop(rb.id, None)

    # an installation from before: the estimate each customer starts with
    from sqlalchemy import text as sql_text
    from app import db as app_db
    async with app_db.engine.begin() as conn:
        await conn.execute(sql_text("UPDATE members SET paid_left = 0 WHERE id = :m"), {"m": payer.id})
        await conn.execute(sql_text(app_db._FILL_NEW_COLUMN[("members", "paid_left")]))
    p, h = await repo.get_member(payer.id), await repo.get_member(hand1.id)
    check("upgrading an older installation: a customer starts with the smaller of their balance and all they paid in",
          p.paid_left == D("10.00") and p.balance == D("12.61") and h.paid_left == 0, f"{p.paid_left} {p.balance}")
    await forget_paid_in()


async def test_email_codes():
    """Email codes in a shop (2026-11-24): a mailbox for one verification mail, sold like
    a number. Email codes were 59% of NumberHub's margin and no shop could sell them."""
    print("email codes")
    from app.bots.callbacks import Em
    from app.bots.reseller import build_router
    own = 5131
    api = FakeNumberHub()
    r = await make_reseller(api, owner=own, bot_id=86501)
    session = FakeSession(bot_id=86501)
    bot = fake_bot(session)
    runtime._bots[r.id] = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(r.id))
    cust = 93001
    m = await member(r, cust, "1.00")
    labels = lambda: [b.text for row in (session.last_markup().inline_keyboard if session.last_markup() else [])  # noqa: E731
                      for b in row]
    sync_now = lambda: selling.sync_reseller(r)                 # noqa: E731

    async def to_site(uid, name):
        await dp.feed_update(bot, msg(uid, "/start"))
        await dp.feed_update(bot, tap(uid, button(session, "Email codes")))
        await dp.feed_update(bot, tap(uid, button(session, name)))

    async def get_box(uid, site_name, domain):
        """The whole way to a mailbox; returns the newest order of that customer."""
        await to_site(uid, site_name)
        await dp.feed_update(bot, tap(uid, button(session, domain)))
        await dp.feed_update(bot, tap(uid, button(session, "Get the address")))
        mem = await repo.get_or_create_member(r.id, uid, None, None)
        rows = await repo.member_orders(mem.id, limit=1)
        return rows[0] if rows else None

    # ── the way in ──
    await dp.feed_update(bot, msg(cust, "/start"))
    check("the menu has an Email codes button", button(session, "Email codes") == Em(a="home").pack())
    await dp.feed_update(bot, tap(cust, button(session, "Email codes")))
    check("the customer is asked which site sends the code, with the most asked for as buttons",
          "Type the website's address" in session.last_text() and labels()[:3] == ["OpenAI", "Google", "Reddit"],
          str(labels()))
    await dp.feed_update(bot, tap(cust, button(session, "OpenAI")))
    check("the mail domains in stock, the reliable first, at the shop's price (NumberHub's plus 30%, rounded up)",
          labels()[:3] == ["✅ gmx.com · $0.04", "✉️ hotmail.com · $0.07", "⚠️ yandex.com · $0.02"]
          and not any("mail.ru" in x for x in labels()), str(labels()))
    await dp.feed_update(bot, tap(cust, button(session, "gmx.com")))
    check("the price is confirmed before anything is bought: charged only when a mail arrives",
          "<b>$0.04</b>" in session.last_text() and "only when a mail arrives" in session.last_text()
          and "<b>$1.00</b>" in session.last_text() and button(session, "Get the address · $0.04") is not None
          and not api.orders)
    await dp.feed_update(bot, tap(cust, button(session, "Get the address")))
    o1 = (await repo.member_orders(m.id, limit=1))[0]
    mm = await repo.get_member(m.id)
    check("a mailbox: its address on the card, the shop's price held, nothing charged",
          o1.kind == "email" and o1.status == "waiting" and o1.phone == f"box{o1.nh_id}@gmx.com"
          and f"<code>{o1.phone}</code>" in session.last_text() and "Enter this address on the site" in session.last_text()
          and mm.balance == D("1.00") and mm.held == D("0.04"), session.last_text()[:200])
    check("…and the card speaks of a mail, not of an SMS or a number",
          "Waiting for the mail" in session.last_text() and "charged only if a mail arrives" in session.last_text()
          and "SMS" not in session.last_text() and "number" not in session.last_text().lower(), session.last_text())
    check("NumberHub was asked for that site and domain at its own price, with the order's key",
          api.email_bodies[-1] == {"site": "openai.com", "domain": "gmx.com", "max_price": "0.03"}
          and api.held == D("0.03") and o1.service == "openai.com" and o1.country == "gmx.com"
          and o1.member_price == D("0.04") and o1.ceiling_at_buy == D("0.03"), str(api.email_bodies[-1]))

    # ── the mail arrives: a code and a link ──
    api.mail_arrives(o1.nh_id, code="482913", link="https://openai.com/verify?t=abc&u=1")
    await sync_now()
    mm = await repo.get_member(m.id)
    told = [x for x in session.texts() if "482913" in x]
    check("the code and the link are sent to the customer, and the price is charged",
          mm.balance == D("0.96") and mm.held == 0 and any("your code is <code>482913</code>" in x
          and "https://openai.com/verify?t=abc&amp;u=1" in x for x in told), str(told[-1:]))
    n = len(session.texts())
    await sync_now()
    await selling.settle_sweep()
    check("a second look neither charges nor tells again",
          (await repo.get_member(m.id)).balance == D("0.96")
          and len([x for x in session.texts()[n:] if "your code is" in x]) == 0)
    await dp.feed_update(bot, tap(cust, Ord(a="view", id=o1.id).pack()))
    check("the finished card shows the code and offers another mailbox",
          "482913" in session.last_text() and button(session, "Another mailbox") == Em(a="home").pack())

    # ── a mail with a link and no code ──
    o2 = await get_box(cust, "Google", "outlook.com")
    api.mail_arrives(o2.nh_id, link="https://accounts.google.com/confirm/xyz")
    await sync_now()
    told = [x for x in session.texts() if "accounts.google.com/confirm/xyz" in x]
    check("a mail that confirms with a link and has no code is delivered too, and charged",
          len(told) == 1 and "sent a mail to" in told[0] and "Open this link to confirm" in told[0]
          and (await repo.get_member(m.id)).balance == D("0.90"), str(told))
    await sync_now()
    check("…once", len([x for x in session.texts() if "accounts.google.com/confirm/xyz" in x]) == 1)
    o3 = await get_box(cust, "OpenAI", "hotmail.com")
    api.mail_arrives(o3.nh_id, message="Welcome!  Your account   is ready.\nNo code here.")
    await sync_now()
    check("a mail with neither: the start of its text",
          any("sent a mail to" in x and "Welcome! Your account is ready. No code here." in x for x in session.texts()))

    # ── no mail: nothing charged ──
    before = (await repo.get_member(m.id)).balance
    o4 = await get_box(cust, "OpenAI", "gmx.com")
    api.set_status(o4.nh_id, "expired")
    await sync_now()
    mm = await repo.get_member(m.id)
    check("no mail arrives: nothing is charged and the customer is told",
          mm.balance == before and mm.held == 0 and (await repo.get_order(o4.id)).settled == Order.RELEASED
          and any("No mail arrived for OpenAI" in x and "back on your balance" in x for x in session.texts()))
    await dp.feed_update(bot, tap(cust, Ord(a="view", id=o4.id).pack()))
    check("…its card says no mail arrived", "No mail arrived" in session.last_text()
          and "No code arrived" not in session.last_text() and "SMS" not in session.last_text())
    await dp.feed_update(bot, tap(cust, Ord(a="view", id=o2.id).pack()))
    check("the card of the mail that had only a link says a mail was received, and what was paid",
          "Mail received" in session.last_text() and "Paid: $0.06" in session.last_text(), session.last_text()[:200])

    # ── cancel ──
    o5 = await get_box(cust, "OpenAI", "gmx.com")
    api.cancel_lock = 97
    await dp.feed_update(bot, tap(cust, Ord(a="cancel", id=o5.id).pack()))
    check("cancelling in the first two minutes is refused with the time left",
          "97" in session.alerts()[-1] and (await repo.get_order(o5.id)).status == "waiting")
    api.cancel_lock = 0
    await dp.feed_update(bot, tap(cust, Ord(a="cancel", id=o5.id).pack()))
    mm = await repo.get_member(m.id)
    check("then it cancels: the mailbox is given back and the hold released",
          (await repo.get_order(o5.id)).status == "canceled" and mm.held == 0 and mm.balance == before
          and ("DELETE", f"/emails/{o5.nh_id}") in api.calls and api.orders[o5.nh_id]["status"] == "canceled")
    o6 = await get_box(cust, "OpenAI", "gmx.com")
    api.cancel_code_first = True
    await dp.feed_update(bot, tap(cust, Ord(a="cancel", id=o6.id).pack()))
    api.cancel_code_first = False
    mm = await repo.get_member(m.id)
    check("a mail that beat the cancel is delivered and charged, not refunded",
          (await repo.get_order(o6.id)).status == "received" and mm.balance == before - D("0.04") and mm.held == 0)

    # ── typing a site ──
    await dp.feed_update(bot, msg(cust, "/start"))
    await dp.feed_update(bot, tap(cust, button(session, "Email codes")))
    await dp.feed_update(bot, msg(cust, "redd"))
    check("typing a name finds the site", labels()[0] == "Reddit" and "Type the website's address" in session.last_text())
    await dp.feed_update(bot, tap(cust, button(session, "Reddit")))
    check("a site with no mailbox in stock says so", "No mailbox is available for <b>Reddit</b>" in session.last_text())
    await dp.feed_update(bot, tap(cust, button(session, "Back")))
    await dp.feed_update(bot, msg(cust, "zzzzqq"))
    check("a name nothing matches: asked again, the list stays",
          "Nothing found for that name" in session.last_text() and len(labels()) >= 2)
    check("the picker says any website can be typed", "Any website works" in session.last_text()
          and "example.com" in session.last_text())

    # ── any website (owner, 2026-11-24: "email must input any website") ──
    check("a typed website is read as its bare address; a plain word is not an address",
          selling.email_site("https://www.MyShop.io/signup?x=1") == "myshop.io" and selling.email_site("@mail.myshop.io") == "mail.myshop.io"
          and selling.email_site("me@myshop.io") == "myshop.io" and selling.email_site("myshop.io/") == "myshop.io"
          and selling.email_site("myshop") is None and selling.email_site("my shop.io") is None
          and selling.email_site("") is None and selling.email_site("-bad.io") is None and selling.email_site("x." + "a" * 300) is None)
    api.email_domains["myshop.io"] = [{"domain": "gmx.com", "price": "0.03", "in_stock": True, "delivery": None}]
    await dp.feed_update(bot, msg(cust, "https://www.MyShop.io/signup"))
    check("a website the list does not know goes straight to its mailboxes, with a spelling check",
          "<b>myshop.io</b>" in session.last_text() and button(session, "gmx.com") is not None
          and "You typed <b>myshop.io</b>" in session.last_text() and "Nothing found" not in session.last_text(),
          session.last_text()[:200])
    await dp.feed_update(bot, tap(cust, button(session, "gmx.com")))
    n_bodies = len(api.email_bodies)
    await dp.feed_update(bot, tap(cust, button(session, "Get the address")))
    typed_order = (await repo.member_orders(m.id, 1))[0]
    check("…and a mailbox is bought for exactly that site",
          len(api.email_bodies) == n_bodies + 1 and api.email_bodies[-1]["site"] == "myshop.io"
          and typed_order.kind == "email" and typed_order.service == "myshop.io" and typed_order.status == "waiting",
          str(api.email_bodies[-1]))
    await selling.cancel(r, await repo.get_member(m.id), typed_order.id)
    await dp.feed_update(bot, msg(cust, "/start"))
    await dp.feed_update(bot, tap(cust, button(session, "Email codes")))
    await dp.feed_update(bot, msg(cust, "openai.com"))
    check("the address of a site the list knows opens it under its name, without the spelling warning",
          "<b>OpenAI</b>" in session.last_text() and "You typed" not in session.last_text()
          and button(session, "gmx.com") is not None)
    await dp.feed_update(bot, tap(cust, button(session, "Back")))
    await dp.feed_update(bot, msg(cust, "nothing-here.example"))
    check("a website with no mailbox right now says so, with a way back",
          "No mailbox is available for <b>nothing-here.example</b>" in session.last_text()
          and button(session, "Back") is not None)

    # ── a price that moved ──
    await to_site(cust, "OpenAI")
    await dp.feed_update(bot, tap(cust, button(session, "gmx.com")))
    stale = button(session, "Get the address")
    api.email_domains["openai.com"][0]["price"] = "0.05"
    orders_before = len(api.orders)
    await dp.feed_update(bot, tap(cust, stale))
    check("the price rose since the screen: nothing is bought, the new price is shown to confirm",
          len(api.orders) == orders_before and "$0.07" in session.last_text()
          and button(session, "Get the address · $0.07") is not None, session.last_text()[:160])
    api.email_domains["openai.com"][0]["price"] = "0.03"

    # ── limits that protect the owner's account ──
    two = 93002
    m2 = await member(r, two, "1.00")
    for _ in range(3):
        await get_box(two, "OpenAI", "yandex.com")
    n_orders = len(api.orders)
    await to_site(two, "OpenAI")
    await dp.feed_update(bot, tap(two, button(session, "yandex.com")))
    await dp.feed_update(bot, tap(two, button(session, "Get the address")))
    check("three mailboxes waiting is the most one customer may have",
          len(api.orders) == n_orders and await repo.member_open_count(m2.id, kind="email") == 3
          and "open" in session.last_text().lower(), session.last_text()[:120])
    keep = (selling.EMAIL_PER_HOUR, selling.EMAIL_SHOP_PER_HOUR)
    try:
        selling.EMAIL_PER_HOUR = await repo.email_orders_since(60, member_id=m.id)
        await to_site(cust, "OpenAI")
        await dp.feed_update(bot, tap(cust, button(session, "gmx.com")))
        await dp.feed_update(bot, tap(cust, button(session, "Get the address")))
        check("a customer who asked for many mailboxes in an hour waits", len(api.orders) == n_orders
              and "many mailboxes in a short time" in session.last_text())
        selling.EMAIL_PER_HOUR = keep[0]
        selling.EMAIL_SHOP_PER_HOUR = await repo.email_orders_since(60, reseller_id=r.id)
        three = await member(r, 93003, "1.00")
        try:
            await selling.buy_email(r, three, "openai.com", "OpenAI", "gmx.com", None)
            limited = ""
        except SellError as e:
            limited = e.reason
        check("…and so does the whole shop at its hourly limit", limited == "email_limit" and len(api.orders) == n_orders)
    finally:
        selling.EMAIL_PER_HOUR, selling.EMAIL_SHOP_PER_HOUR = keep

    # ── mailboxes that never get a mail ──
    idle = await member(r, 93005, "1.00")
    for n in range(selling.EMAIL_WASTED_MAX):
        box = await selling.buy_email(r, idle, "openai.com", "OpenAI", "yandex.com", None)
        api.set_status(box.nh_id, "expired")
        await sync_now()
    try:
        await selling.buy_email(r, idle, "openai.com", "OpenAI", "yandex.com", None)
        stopped = ""
    except SellError as e:
        stopped = e.reason
    check("six mailboxes in a day that got no mail, and none that did: the customer waits",
          stopped == "email_limit" and await repo.email_outcomes(idle.id) == (6, 0)
          and (await repo.get_member(idle.id)).balance == D("1.00"), f"{stopped} {await repo.email_outcomes(idle.id)}")
    user = await member(r, 93006, "1.00")
    box = await selling.buy_email(r, user, "openai.com", "OpenAI", "yandex.com", None)
    api.mail_arrives(box.nh_id, code="111000")
    await sync_now()
    for n in range(selling.EMAIL_WASTED_MAX):
        box = await selling.buy_email(r, user, "openai.com", "OpenAI", "yandex.com", None)
        api.set_status(box.nh_id, "expired")
        await sync_now()
    box = await selling.buy_email(r, user, "openai.com", "OpenAI", "yandex.com", None)
    check("…a customer whose mailboxes do get mail is not stopped by the ones that did not",
          box.status == "waiting" and await repo.email_outcomes(user.id) == (6, 1))

    # ── a lost reply never opens two mailboxes ──
    four = await member(r, 93004, "1.00")
    api.lose_replies = 3
    try:
        await selling.buy_email(r, four, "openai.com", "OpenAI", "gmx.com", None)
        lost = ""
    except SellError as e:
        lost = e.reason
    stuck = [x for x in await repo.member_orders(four.id, 5) if x.status == Order.BUYING]
    check("three lost replies: 'processing', the hold is kept, the order waits",
          lost == "processing" and len(stuck) == 1 and stuck[0].kind == "email"
          and (await repo.get_member(four.id)).held == D("0.04"))
    n_orders = len(api.orders)
    orig = repo.stale_buying
    repo.stale_buying = lambda older_than_sec=60: orig(older_than_sec=0)
    try:
        await selling.recover_buying()
    finally:
        repo.stale_buying = orig
    rec = await repo.get_order(stuck[0].id)
    check("recovery asks again with the same request: the ONE mailbox NumberHub opened is linked",
          rec.status == "waiting" and rec.nh_id in api.orders and len(api.orders) == n_orders
          and rec.phone == api.orders[rec.nh_id]["phone"], f"{rec.status} {len(api.orders)} {n_orders}")

    # ── the customer's order list ──
    await dp.feed_update(bot, msg(cust, "/orders"))
    check("email orders are in the customer's list, marked as such",
          any("📧 OpenAI" in x for x in labels()) and not any("+box" in x for x in labels()), str(labels()[:3]))

    # ── the owner's switch ──
    from app.bots.callbacks import Adm
    await dp.feed_update(bot, msg(own, "/admin"))
    check("the Admin panel says email codes are on and has the switch",
          "📧 Email codes: <b>on</b>" in session.last_text() and button(session, "Email codes: on") == Adm(a="email", arg="off").pack())
    await dp.feed_update(bot, tap(own, button(session, "Email codes: on")))
    off = await repo.get_reseller(r.id)
    check("one tap switches them off", off.email_on == 0 and not selling.email_on(off)
          and button(session, "Email codes: off") is not None)
    await dp.feed_update(bot, msg(cust, "/start"))
    check("…and the customers' menu no longer has the button", button(session, "Email codes") is None)
    await dp.feed_update(bot, tap(cust, Em(a="home").pack()))
    check("…an old button says sales are paused", "paused" in session.alerts()[-1].lower())
    try:
        await selling.buy_email(off, m, "openai.com", "OpenAI", "gmx.com", None)
        refused = ""
    except SellError as e:
        refused = e.reason
    check("…and nothing can be bought", refused == "paused")
    await dp.feed_update(bot, msg(own, "/admin"))
    await dp.feed_update(bot, tap(own, button(session, "Email codes: off")))
    check("switched on again", selling.email_on(await repo.get_reseller(r.id)))

    # ── an API without email codes: no button ──
    api2 = FakeNumberHub()
    api2.email_has = False
    r2 = await make_reseller(api2, owner=5132, bot_id=86502)
    session2 = FakeSession(bot_id=86502)
    bot2 = fake_bot(session2)
    runtime._bots[r2.id] = bot2
    dp2 = Dispatcher(storage=MemoryStorage())
    dp2.include_router(build_router(r2.id))
    await dp2.feed_update(bot2, msg(93010, "/start"))
    check("a shop whose API lists no email sites shows no Email codes button", button(session2, "Email codes") is None
          and button(session2, "Buy") is not None)

    # ── every language ──
    keys = ["btn_email", "email_pick_site", "email_none_found", "email_type_any", "email_typed_site",
            "email_pick_domain", "email_no_domains",
            "email_confirm", "btn_email_buy", "email_card_title", "email_card_address", "email_card_howto",
            "email_msg_code", "email_msg_mail", "email_msg_link", "err_email_limit", "btn_email_again",
            "email_st_waiting", "email_st_received", "email_st_expired", "email_card_held", "email_msg_refunded",
            "email_st_buying", "email_st_failed"]
    missing = [(lang, k) for lang in LANGS for k in keys if k not in _table(lang)]
    same = [(lang, k) for lang in LANGS if lang != "en" for k in keys
            if k not in ("email_card_title", "email_card_address") and _table(lang).get(k) == _table("en")[k]]
    check("the email texts exist in all 14 languages, translated", not missing and not same, f"{missing[:3]} {same[:3]}")
    runtime._bots.pop(r.id, None)
    runtime._bots.pop(r2.id, None)


async def test_paused_shop():
    print("a paused shop")
    from app.bots.reseller import build_router
    api = FakeNumberHub()
    r = await make_reseller(api, bot_id=795)
    m = await member(r, ALICE + 300, "3")
    order = await selling.buy(r, m, "wa", "187", None)
    await repo.set_card(order.id, ALICE + 300, 77)
    await repo.update_reseller(r.id, status=Reseller.DISABLED)
    r = await repo.get_reseller(r.id)
    session = FakeSession()
    bot = fake_bot(session)
    runtime._bots[r.id] = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(r.id))
    await dp.feed_update(bot, msg(ALICE + 300, "/start"))
    check("customers are told the shop is paused", "paused" in session.last_text())
    try:
        await selling.buy(r, await repo.get_member(m.id), "wa", "187", None)
        check("no new sales while paused", False)
    except SellError as e:
        check("no new sales while paused", e.reason == "paused")
    await dp.feed_update(bot, tap(ALICE + 300, Ord(a="view", id=order.id).pack()))
    check("…but a customer can still open the order they have", "+1555000" in session.last_text())
    api.set_status(order.nh_id, "received", "778899")
    await selling.sync_reseller(r)
    mm = await repo.get_member(m.id)
    check("…and its code is still delivered and charged once",
          any("778899" in x for x in session.texts()) and mm.held == 0 and mm.balance == D("3") - D("0.39"))
    runtime._bots.pop(r.id, None)


def _fast_recovery():
    """recover_buying() without its 60 s age gate (the tests can't wait)."""
    orig = repo.stale_buying

    def patched(older_than_sec=60):
        return orig(older_than_sec=0)
    return orig, patched


async def _recover_now():
    orig, patched = _fast_recovery()
    repo.stale_buying = patched
    try:
        await selling.recover_buying()
    finally:
        repo.stale_buying = orig


async def test_review_money():
    print("review: an unclear purchase answer never releases the hold (2026-10-02)")
    api = FakeNumberHub()
    r = await make_reseller(api, bot_id=801)
    session = FakeSession()
    runtime._bots[r.id] = fake_bot(session)
    m = await member(r, ALICE + 400, "5")

    # NumberHub bought it, the reply was lost, the retries hit "in progress".
    api.lose_replies, api.in_progress_next = 1, 2
    try:
        await selling.buy(r, m, "wa", "187", None)
        check("lost reply + in progress -> 'processing'", False)
    except SellError as e:
        o = [x for x in await repo.member_orders(m.id, 5)][0]
        check("lost reply + in progress -> 'processing', hold kept, order BUYING",
              e.reason == "processing" and o.status == Order.BUYING
              and (await repo.get_member(m.id)).held == D("0.39") and len(api.orders) == 1, e.reason)
    await _recover_now()
    o = await repo.get_order(o.id)
    check("recovery links the number NumberHub bought (no second purchase)",
          o.status == "waiting" and len(api.orders) == 1 and o.nh_id in api.orders)
    check("…and sends the member the number they were told is processing",
          any("+1555000" in x for x in session.texts()) and o.message_id is not None)

    # The same, with 429s on the retries and on the first recovery attempt.
    api.lose_replies, api.then_rate_limit = 1, 2
    try:
        await selling.buy(r, await repo.get_member(m.id), "tg", "187", None)
    except SellError as e:
        check("lost reply + 429 -> 'processing', not a release", e.reason == "processing")
    o2 = [x for x in await repo.member_orders(m.id, 5) if x.service == "tg"][0]
    api.rate_limit_next = 1
    await _recover_now()
    check("a 429 on the recovery leaves it BUYING with its hold (it proves nothing)",
          (await repo.get_order(o2.id)).status == Order.BUYING
          and (await repo.get_member(m.id)).held == D("0.39") + D("1.43"))
    await _recover_now()
    o2 = await repo.get_order(o2.id)
    check("the next sweep links it: ONE number for it at NumberHub", o2.status == "waiting" and len(api.orders) == 2)

    # A failed row whose purchase turns out to exist: re-held, or given back.
    m2 = await member(r, ALICE + 401, "1")
    row = await repo.create_order_with_hold(reseller_id=r.id, member_id=m2.id, service="wa", service_name="WhatsApp",
                                            country="187", member_price=D("0.39"), markup_pct_at_buy=D("30"))
    await repo.fail_order(row.id)
    _, made = api._do_buy({"service": "wa", "country": "187", "max_price": "0.30"})
    check("link refuses a row that already gave its hold back", not await repo.link_order(row.id, made["number"]))
    check("…the late purchase is taken on again with a fresh hold",
          await selling._attach(r, row.id, made["number"])
          and (await repo.get_order(row.id)).status == "waiting"
          and (await repo.get_order(row.id)).settled == Order.UNSETTLED
          and (await repo.get_member(m2.id)).held == D("0.39"))
    m3 = await member(r, ALICE + 402, "0.39")
    row3 = await repo.create_order_with_hold(reseller_id=r.id, member_id=m3.id, service="wa", service_name="WhatsApp",
                                             country="187", member_price=D("0.39"), markup_pct_at_buy=D("30"))
    await repo.fail_order(row3.id)
    await repo.member_adjust(r.id, m3.id, D("-0.39"))
    _, made3 = api._do_buy({"service": "wa", "country": "187", "max_price": "0.30"})
    nh3 = made3["number"]["id"]
    ok3 = await selling._attach(r, row3.id, made3["number"])
    check("…and when the member can't cover it any more, the number is given back at NumberHub",
          not ok3 and api.orders[nh3]["status"] == "canceled" and (await repo.get_order(row3.id)).status == Order.FAILED
          and (await repo.get_member(m3.id)).held == 0)

    # A double tap buys once.
    m4 = await member(r, ALICE + 403, "5")
    api.lose_replies = 1                                  # slows the first buy down
    res = await asyncio.gather(selling.buy(r, m4, "wa", "6", None), selling.buy(r, m4, "wa", "6", None),
                               return_exceptions=True)
    dup = [x for x in res if isinstance(x, SellError) and x.reason == "duplicate"]
    check("a double tap on Buy buys ONE number", len(dup) == 1 and
          len([o for o in await repo.member_orders(m4.id, 5)]) == 1)

    # Old orders past NumberHub's 24 h key memory are never replayed.
    m5 = await member(r, ALICE + 404, "1")
    old = await repo.create_order_with_hold(reseller_id=r.id, member_id=m5.id, service="wa", service_name="WhatsApp",
                                            country="187", member_price=D("0.39"), markup_pct_at_buy=D("30"))
    from sqlalchemy import update as _upd
    from app.db import session_factory
    async with session_factory() as s:
        await s.execute(_upd(Order).where(Order.id == old.id)
                        .values(created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=21)))
        await s.commit()
    posts = len([c for c in api.calls if c[0] == "POST"])
    await _recover_now()
    check("a BUYING row older than 20 h is released, not replayed",
          (await repo.get_order(old.id)).status == Order.FAILED and (await repo.get_member(m5.id)).held == 0
          and len([c for c in api.calls if c[0] == "POST"]) == posts)

    # Balances are compared in cents: exactly the price available is enough.
    m6 = await member(r, ALICE + 405, "0.30")
    await repo.member_try_hold(m6.id, D("0.10"))
    check("0.30 balance, 0.10 held: a 0.20 hold goes through", await repo.member_try_hold(m6.id, D("0.20")))
    runtime._bots.pop(r.id, None)


async def test_review_sync():
    print("review: sync never reopens, re-edits or drops a code")
    api = FakeNumberHub()
    r = await make_reseller(api, bot_id=802)
    session = FakeSession()
    runtime._bots[r.id] = fake_bot(session)
    m = await member(r, ALICE + 500, "5")
    o = await selling.buy(r, m, "wa", "187", None)
    await repo.set_card(o.id, ALICE + 500, 70)
    stale = dict(api.public(api.orders[o.nh_id]))         # a list fetched before the cancel
    ok, _, _ = await selling.cancel(r, await repo.get_member(m.id), o.id)
    await repo.apply_nh_state(o.id, stale)
    check("a stale 'waiting' snapshot can't reopen a cancelled order",
          ok and (await repo.get_order(o.id)).status == "canceled")

    o2 = await selling.buy(r, await repo.get_member(m.id), "wa", "187", None)
    await repo.set_card(o2.id, ALICE + 500, 71)
    o3 = await selling.buy(r, await repo.get_member(m.id), "wa", "6", None)
    await repo.set_card(o3.id, ALICE + 500, 71)           # 📱 New number drawn on the same message
    check("a message shows one order: the older one lets go of it",
          (await repo.get_order(o2.id)).message_id is None and (await repo.get_order(o3.id)).message_id == 71)

    session.fail_send = 1                                  # Telegram's flood limit on the code message
    api.set_status(o3.nh_id, "received", "556677")
    await selling.sync_reseller(r)
    check("a code message that hit a flood limit is not marked sent",
          (await repo.get_order(o3.id)).codes_announced == 0)
    await selling.sync_reseller(r)
    check("…and is sent on the next pass", (await repo.get_order(o3.id)).codes_announced == 1
          and any("556677" in x for x in session.texts()))
    from aiogram.methods import EditMessageText
    before = len([x for x in session.requests if isinstance(x, EditMessageText)])
    await selling.sync_reseller(r)
    await selling.sync_reseller(r)
    after = len([x for x in session.requests if isinstance(x, EditMessageText)])
    check("a delivered card is not re-edited on every 5 s pass", after == before, f"{after - before} edits")

    from app.texts import code_arrived
    cur = await repo.get_order(o3.id)
    flash = code_arrived(await repo.get_member(m.id), cur, "441616961154")
    check("a code that is a caller's number tells the member to use its last digits",
          "961154" in flash and "1154" in flash and "call" in flash)
    check("…a normal code gets no such hint", "call" not in code_arrived(await repo.get_member(m.id), cur, "556677"))
    api.orders[o3.nh_id]["status"] = "requesting"         # "another code" pressed in NumberHub's own app
    await selling.sync_reseller(r)
    cur = await repo.get_order(o3.id)
    from app.texts import order_card
    check("an unknown NumberHub status keeps being polled and reads as a delivered card",
          cur.status == "requesting" and cur.id in {x.id for x in await repo.recently_received(r.id)}
          and "st_requesting" not in order_card(await repo.get_member(m.id), cur)[0])
    runtime._bots.pop(r.id, None)


async def test_review_bots():
    print("review: bot screens and the builder")
    from app.bots import builder
    from app.bots.reseller import build_router
    api = FakeNumberHub()
    r = await make_reseller(api, bot_id=803)
    session = FakeSession()
    bot = fake_bot(session)
    runtime._bots[r.id] = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(r.id))
    group = Update.model_validate({"update_id": next(_uid), "message": {
        "message_id": next(_uid), "date": 0, "chat": {"id": -100123, "type": "supergroup", "title": "g"},
        "from": _user(ALICE + 600), "text": "/start",
        "entities": [{"type": "bot_command", "offset": 0, "length": 6}]}})
    n = len(session.requests)
    await dp.feed_update(bot, group)
    check("a group chat gets nothing (cards would show numbers and codes to the group)", len(session.requests) == n)

    await member(r, ALICE + 601, "1")
    await dp.feed_update(bot, msg(OWNER, "/admin"))
    await dp.feed_update(bot, tap(OWNER, Adm(a="broadcast").pack()))
    await dp.feed_update(bot, msg(OWNER, "/buy"))
    await dp.feed_update(bot, msg(OWNER, "whatsapp"))
    sent_to_customer = [x for x in session.requests if type(x).__name__ == "SendMessage" and x.chat_id == ALICE + 601]
    check("an open broadcast prompt ends when the owner does something else (no accidental broadcast)",
          not sent_to_customer and "Results for" in session.last_text())
    await dp.feed_update(bot, tap(OWNER, Adm(a="markup", arg="sms").pack()))
    check("commission prompt shows plain numbers (no '3E+1')", "E+" not in session.last_text()
          and "Now: <b>" in session.last_text())
    runtime._bots.pop(r.id, None)

    # Builder: reconnect the same bot with a new token, ration bad keys, check scopes.
    bsession = FakeSession(bot_id=803, username="my_shop_bot")
    bbot = fake_bot(bsession)
    bdp = Dispatcher(storage=MemoryStorage())
    builder.router._parent_router = None     # test_builder attached it to its own dispatcher
    bdp.include_router(builder.router)
    orig_make, orig_restart, orig_nh = runtime.make_bot, runtime.restart, builder.NumberHub
    restarted = []
    runtime.make_bot = lambda token: fake_bot(FakeSession(bot_id=803, username="my_shop_bot"))

    async def fake_restart(rid):
        restarted.append(rid)
    runtime.restart = fake_restart
    builder.NumberHub = lambda key: api.client(key)
    try:
        await bdp.feed_update(bbot, tap(OWNER, "bd:create:0:"))
        await bdp.feed_update(bbot, msg(OWNER, "123456789:" + "C" * 35))
        check("the same bot with a new token reconnects (no 'already connected')",
              "Reconnecting" in bsession.last_text())
        await bdp.feed_update(bbot, msg(OWNER, api.key))
        rows = await repo.list_resellers(owner_id=OWNER)
        rr = await repo.get_reseller(r.id)
        check("…the same shop row keeps its customers, with the new token",
              restarted == [r.id] and crypto.decrypt(rr.bot_token_enc).endswith("C" * 35)
              and len([x for x in rows if x.bot_id == 803]) == 1)
        await bdp.feed_update(bbot, tap(OWNER + 1, "bd:create:0:"))
        await bdp.feed_update(bbot, msg(OWNER + 1, "123456789:" + "D" * 35))
        check("someone else can't take the bot over", "someone else" in bsession.last_text())
        builder._key_fails.clear()
        builder._all_fails.clear()
        await bdp.feed_update(bbot, tap(OWNER + 2, "bd:create:0:"))
        runtime.make_bot = lambda token: fake_bot(FakeSession(bot_id=904, username="other_bot"))
        await bdp.feed_update(bbot, msg(OWNER + 2, "123456789:" + "E" * 35))
        for i in range(3):
            await bdp.feed_update(bbot, msg(OWNER + 2, f"nh_live_wrongkey{i}-0123456789abc"))
        calls = len(api.calls)
        await bdp.feed_update(bbot, msg(OWNER + 2, "nh_live_wrongkey9-0123456789abc"))
        check("bad keys are rationed: the 4th never reaches NumberHub (it would block the server's IP)",
              "Too many keys" in bsession.last_text() and len(api.calls) == calls)
        builder._key_fails.clear()
        api.scopes.discard("orders:read")
        await bdp.feed_update(bbot, msg(OWNER + 2, api.key))
        check("a key without a needed permission is refused, naming it",
              "missing a permission" in bsession.last_text() and "orders:read" in bsession.last_text())
        api.scopes.add("orders:read")
        await bdp.feed_update(bbot, msg(OWNER + 2, api.key))
        check("…with every permission it is accepted", "Step 3 of 3" in bsession.last_text())
        await bdp.feed_update(bbot, tap(OWNER + 2, "bd:markup:0:-50"))
        check("a forged commission button is refused", not await repo.list_resellers(owner_id=OWNER + 2))
        # New API key from another NumberHub account while the shop has open orders.
        await member(r, ALICE + 602, "2")
        await selling.buy(r, await repo.find_member(r.id, str(ALICE + 602)), "wa", "187", None)
        other = FakeNumberHub(key="nh_live_otheraccount-0123456789")
        builder.NumberHub = lambda key: (other if key == other.key else api).client(key)
        await bdp.feed_update(bbot, tap(OWNER, f"bd:newkey:{r.id}:"))
        await bdp.feed_update(bbot, msg(OWNER, other.key))
        check("a key from another NumberHub account is refused while orders are open",
              "different NumberHub account" in bsession.last_text()
              and crypto.decrypt((await repo.get_reseller(r.id)).api_key_enc) == api.key)
        await bdp.feed_update(bbot, msg(OWNER + 3, "123456789:" + "F" * 35))
        check("a token pasted outside the steps starts the flow and is deleted",
              any(type(x).__name__ == "DeleteMessage" for x in bsession.requests[-6:]))
    finally:
        runtime.make_bot, runtime.restart, builder.NumberHub = orig_make, orig_restart, orig_nh
        builder._key_fails.clear()
        builder._all_fails.clear()


async def test_review_runtime():
    print("review: a shop survives a failed start")
    api = FakeNumberHub()
    r = await make_reseller(api, bot_id=804)
    session = FakeSession()
    bot = fake_bot(session)
    dp = Dispatcher(storage=MemoryStorage())
    attempts = []

    async def flaky_polling(*a, **k):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("getMe timed out at boot")
    dp.start_polling = flaky_polling
    real_sleep = asyncio.sleep

    async def no_wait(_s, *a, **k):
        await real_sleep(0)
    asyncio.sleep = no_wait
    try:
        await asyncio.wait_for(runtime._run(r.id, bot, dp), timeout=5)
    finally:
        asyncio.sleep = real_sleep
    check("polling that fails once is retried, not left dead", len(attempts) == 2)
    check("…and an old webhook is removed before polling",
          any(type(x).__name__ == "DeleteWebhook" for x in session.requests))
    check("orders of a shop are synced even when its bot isn't polling", r.id in runtime.known())


async def test_cards_render():
    print("order card in every status and language")
    from app.texts import order_card
    from app.models import Member
    now = dt.datetime.now(dt.timezone.utc)
    errs = []
    for lang in LANGS:
        mem = Member(id=1, reseller_id=1, telegram_id=1, language=lang, balance=D("1"), held=D("0"))
        for st in ("buying", "pending", "waiting", "received", "completed", "canceled", "expired", "failed"):
            o = Order(id=1, reseller_id=1, member_id=1, service="wa", service_name="WhatsApp", country="187",
                      country_name="USA", country_iso="US", phone="15551234" if st != "pending" else None,
                      member_price=D("0.39"), status=st, codes='["123456"]' if st in ("received", "completed") else "[]",
                      expires_at=now + dt.timedelta(minutes=19), cancel_available_at=now + dt.timedelta(seconds=40))
            try:
                text, kb = order_card(mem, o)
                if "{" in text or not kb.inline_keyboard:
                    errs.append(f"{lang}/{st}")
            except Exception as exc:  # noqa: BLE001
                errs.append(f"{lang}/{st}: {exc}")
    check("renders cleanly everywhere", not errs, str(errs[:5]))


async def test_small_topups_and_rupiah():
    """A shop owner in Indonesia (2026-11-24): top-ups from $0.50, amounts typed in
    rupiah ("13,000" was read as $13.00), a top-up screen that says dollars and shows
    rupiah, and the owner's own note on it."""
    print("small top-ups, rupiah amounts, a clearer top-up screen")
    from aiogram.methods import SendPhoto
    from app import fx, payments
    from app.bots.callbacks import Pay
    from app.bots.reseller import build_router
    from app.models import Payment
    from fakes import FakeFx, FakeGateways
    gw = FakeGateways()
    gw.next = 7000                    # payment ids of its own: the QRIS test before this one starts at 1 too
    payments.TRANSPORT = gw.transport()
    fakefx = FakeFx()
    fx.TRANSPORT, fx._rate = fakefx.transport(), None
    await fx.refresh()
    api = FakeNumberHub()
    r = await make_reseller(api, bot_id=86451)
    session = FakeSession()
    bot = fake_bot(session)
    runtime._bots[r.id] = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(r.id))
    photos = lambda uid: [m for m in session.requests if isinstance(m, SendPhoto) and m.chat_id == uid]  # noqa: E731

    # ── how a typed amount is read ──
    want = {"15": (None, "15", False), "0.5": (None, "0.5", False), "0,5": (None, "0.5", False),
            "2.50": (None, "2.50", False), "$2": ("usd", "2", False), "2 usd": ("usd", "2", False),
            "13,000": (None, "13000", True), "13.000": (None, "13000", True), "13000": (None, "13000", False),
            "5 000": (None, "5000", True), "Rp 13.000": ("idr", "13000", True), "rp13.000": ("idr", "13000", True),
            "13000 IDR": ("idr", "13000", False), "13,000.IDR": ("idr", "13000", True), "13rb": ("idr", "13000", False),
            "13 ribu": ("idr", "13000", False), "13k": ("idr", "13000", False), "13.5k": ("idr", "13500", False),
            "1,5jt": ("idr", "1500000", False), "1.000.000": (None, "1000000", True), "0.500": (None, "0.5", False)}
    got = {raw: payments.read_amount(raw) for raw in want}
    bad = [raw for raw, (cur, num, grp) in want.items()
           if got[raw] is None or got[raw][0] != cur or got[raw][1] != D(num) or got[raw][2] != grp]
    check("typed amounts are read as written: thousands are thousands, rupiah words name rupiah", not bad,
          str([(raw, got[raw]) for raw in bad]))
    check("what is not an amount is not guessed",
          all(payments.read_amount(x) is None for x in ("abc", "$5 idr", "-5", "IDR", "2 50", "", "1e5", "5..0")))
    check("a shop with no way to pay keeps the old range", payments.limits(r) == (D("1"), D("1000")))

    async def top_up(uid):
        await dp.feed_update(bot, msg(uid, "/start"))
        await dp.feed_update(bot, tap(uid, button(session, "Balance")))
        await dp.feed_update(bot, tap(uid, button(session, "Add balance")))

    async def typed(uid, text):
        """Other amount -> the text. Returns (QRIS payments made by it, the answer)."""
        before = len(gw.ks_bodies)
        await top_up(uid)
        await dp.feed_update(bot, tap(uid, button(session, "Other amount")))
        await dp.feed_update(bot, msg(uid, text))
        return len(gw.ks_bodies) - before, session.last_text()

    # ── a shop paid by QRIS only ──
    await dp.feed_update(bot, msg(OWNER, "/admin"))
    await dp.feed_update(bot, tap(OWNER, button(session, "Payments")))
    await dp.feed_update(bot, tap(OWNER, button(session, "Connect 🇮🇩 KaseraPay")))
    await dp.feed_update(bot, msg(OWNER, gw.ks_key))
    await repo.update_reseller(r.id, kasera_rate=D("16500"), kasera_auto=0)
    r = await repo.get_reseller(r.id)
    check("QRIS starts at $0.50 (the gateway's own floor is Rp 5.000) and ends at what one QR can carry",
          payments.limits(r) == (D("0.50"), D("60.60")) and payments.method_limits(r, payments.KASERA) == (D("0.50"), D("60.60"))
          and payments.presets(r) == [D("0.5"), D("1"), D("2"), D("5"), D("10"), D("20"), D("50")], str(payments.limits(r)))
    await top_up(9901)
    text = session.last_text()
    check("the screen says how THIS shop is paid, that amounts are dollars, and what a dollar is in rupiah",
          "You pay in rupiah by QRIS" in text and "crypto" not in text and "Amounts are in US dollars ($)." in text
          and "Here $1.00 = Rp 16.500." in text and "Minimum: <b>$0.50</b>" in text, text[:300])
    check("each amount button carries its rupiah, from $0.50 up to what one QR can carry",
          button(session, "$0.50 · Rp 8.250") is not None and button(session, "$50.00 · Rp 825.000") is not None
          and button(session, "$100") is None)
    await dp.feed_update(bot, tap(9901, button(session, "$0.50")))
    check("a $0.50 top-up: a QR for Rp 8.250", len(photos(9901)) == 1 and "Rp 8.250" in photos(9901)[0].caption
          and "$0.50" in photos(9901)[0].caption and gw.ks_bodies[-1]["amount"] == 8250)
    await top_up(9902)
    await dp.feed_update(bot, tap(9902, button(session, "Other amount")))
    check("'Other amount' asks for dollars or rupiah, with an example of each",
          "in dollars" in session.last_text() and "or in rupiah" in session.last_text()
          and "Rp 30.000" in session.last_text() and "Minimum: <b>$0.50</b>" in session.last_text())
    await dp.feed_update(bot, msg(9902, "13.000"))
    mem = await repo.get_or_create_member(r.id, 9902, None, None)
    p = (await repo.open_payments(mem.id))[0]
    check("'13.000' is Rp 13.000, not $13: the QR asks exactly that and adds its dollars rounded down",
          len(photos(9902)) == 1 and "Rp 13.000" in photos(9902)[0].caption and "$0.78" in photos(9902)[0].caption
          and gw.ks_bodies[-1]["amount"] == 13000 and p.amount == D("0.78") and payments.details(p)["idr"] == 13000,
          str(photos(9902)[0].caption if photos(9902) else session.last_text())[:160])
    gw.ks[p.invoice_id]["status"] = "succeeded"
    await asyncio.gather(payments.poll_all(), payments.poll_all())
    check("paid: exactly $0.78 is added, once",
          (await repo.get_member(mem.id)).balance == D("0.78") and (await repo.get_payment(p.id)).status == Payment.PAID)
    forms = {"13,000": 13000, "Rp 50.000": 50000, "13rb": 13000, "13k": 13000, "100000 idr": 100000,
             "13,000.IDR": 13000, "25 ribu": 25000}
    wrong = []
    for i, (text, idr) in enumerate(forms.items()):
        made, _ = await typed(9910 + i, text)
        if made != 1 or gw.ks_bodies[-1]["amount"] != idr or f"Rp {idr:,}".replace(",", ".") not in photos(9910 + i)[-1].caption:
            wrong.append((text, made, gw.ks_bodies[-1]["amount"]))
    check("every way of writing rupiah makes a QR for exactly those rupiah", not wrong, str(wrong))
    made, _ = await typed(9920, "0,5")
    check("dollars still work, with a comma too: $0.50", made == 1 and gw.ks_bodies[-1]["amount"] == 8250)
    made, text = await typed(9921, "4.000")
    check("under the gateway's Rp 5.000: asked again with both ranges, nothing opened",
          made == 0 and "from <b>$0.50</b> to <b>$60.60</b>" in text and "from <b>Rp 8.250</b> to <b>Rp 1.000.000</b>" in text,
          text[:200])
    for text in ("5.000", "2.000.000", "0.40", "100", "$5 idr", "abc", "Rp 13.000,50"):
        made, answer = await typed(9921, text)
        if made or "from <b>$0.50</b>" not in answer:
            wrong.append((text, made, answer[:60]))
    check("too little, too much, two currencies, not a number: asked again, nothing opened", not wrong, str(wrong))

    # ── the owner's own minimum and note ──
    await dp.feed_update(bot, msg(OWNER, "/admin"))
    await dp.feed_update(bot, tap(OWNER, button(session, "Payments")))
    check("the owner sees what applies now", "from <b>$0.50</b> to <b>$60.60</b> at a time" in session.last_text()
          and button(session, "Note for customers") is not None)
    await dp.feed_update(bot, tap(OWNER, button(session, "Minimum")))
    check("…and is told the floors when setting a minimum", "<b>$0.50</b> by QRIS" in session.last_text())
    await dp.feed_update(bot, msg(OWNER, "2"))
    r = await repo.get_reseller(r.id)
    check("the owner's own minimum counts when it is higher", payments.limits(r) == (D("2.00"), D("60.60"))
          and payments.presets(r) == [D("5"), D("10"), D("20"), D("50")])
    made, text = await typed(9922, "Rp 20.000")
    check("…for rupiah too: Rp 20.000 is $1.21, under $2", made == 0 and "from <b>Rp 33.000</b>" in text, text[:200])
    await dp.feed_update(bot, msg(OWNER, "/admin"))
    await dp.feed_update(bot, tap(OWNER, button(session, "Payments")))
    await dp.feed_update(bot, tap(OWNER, button(session, "Minimum")))
    await dp.feed_update(bot, msg(OWNER, "-"))
    await dp.feed_update(bot, tap(OWNER, button(session, "Note for customers")))
    check("the note is explained", "up to 300 characters" in session.last_text())
    await dp.feed_update(bot, msg(OWNER, "x" * 301))
    check("a note that is too long is asked again", "Up to 300 characters" in session.last_text()
          and (await repo.get_reseller(r.id)).pay_note is None)
    await dp.feed_update(bot, msg(OWNER, "Type rupiah like 20.000 <b>or</b> tap an amount"))
    check("the note is saved and shown back to the owner as plain text",
          (await repo.get_reseller(r.id)).pay_note == "Type rupiah like 20.000 <b>or</b> tap an amount"
          and "Your note on the top-up screen" in session.last_text() and "&lt;b&gt;or&lt;/b&gt;" in session.last_text())
    await top_up(9923)
    check("customers see the note on the top-up screen, as plain text",
          "Type rupiah like 20.000 &lt;b&gt;or&lt;/b&gt; tap an amount" in session.last_text())
    await dp.feed_update(bot, msg(OWNER, "/admin"))
    await dp.feed_update(bot, tap(OWNER, button(session, "Payments")))
    await dp.feed_update(bot, tap(OWNER, button(session, "Note for customers")))
    await dp.feed_update(bot, msg(OWNER, "-"))
    await top_up(9923)
    check("'-' removes it", (await repo.get_reseller(r.id)).pay_note is None and "Type rupiah" not in session.last_text())

    # ── QRIS and crypto in one shop ──
    await dp.feed_update(bot, msg(OWNER, "/admin"))
    await dp.feed_update(bot, tap(OWNER, button(session, "Payments")))
    await dp.feed_update(bot, tap(OWNER, button(session, "Connect 🪙 OxaPay")))
    await dp.feed_update(bot, msg(OWNER, gw.ox_key))
    r = await repo.get_reseller(r.id)
    n_ox = len(gw.ox)
    check("with crypto too the shop takes $0.50 to $1000", payments.limits(r) == (D("0.50"), D("1000"))
          and not payments.takes(r, payments.OXAPAY, D("0.50")) and payments.takes(r, payments.OXAPAY, D("1"))
          and not payments.takes(r, payments.KASERA, D("100")))
    await top_up(9930)
    text = session.last_text()
    check("the screen names both ways; the buttons are plain dollars",
          "by QRIS (rupiah) or by crypto" in text and "Here $1.00 = Rp 16.500." in text
          and button(session, "$0.50") is not None and button(session, "Rp") is None, text[:260])
    await dp.feed_update(bot, tap(9930, button(session, "$0.50")))
    check("$0.50 can only be QRIS: the QR comes at once, no crypto link is made",
          len(photos(9930)) == 1 and "Rp 8.250" in photos(9930)[0].caption and len(gw.ox) == n_ox)
    await top_up(9931)
    await dp.feed_update(bot, tap(9931, button(session, "$5.00")))
    check("$5.00: the customer picks, and the QRIS button says the rupiah",
          button(session, "QRIS (Rupiah) · Rp 82.500") is not None and button(session, "BTC, ETH, USDT") is not None)
    made, _ = await typed(9932, "100")
    check("$100 is over what one QR carries: straight to the crypto link",
          made == 0 and len(gw.ox) == n_ox + 1 and "$100.00" in session.last_text())
    made, _ = await typed(9933, "Rp 50.000")
    check("rupiah typed here still makes a QR", made == 1 and gw.ks_bodies[-1]["amount"] == 50000 and len(gw.ox) == n_ox + 1)
    n_ks = len(gw.ks_bodies)
    await dp.feed_update(bot, tap(9934, Pay(a="via", g="ox", c=50).pack()))
    await dp.feed_update(bot, tap(9934, Pay(a="via", g="kq", c=10000).pack()))
    check("a button made by hand for an amount a way cannot take opens nothing",
          len(gw.ox) == n_ox + 1 and len(gw.ks_bodies) == n_ks)

    # ── a shop paid by crypto only ──
    r2 = await make_reseller(FakeNumberHub(), bot_id=86452)
    session2 = FakeSession()
    bot2 = fake_bot(session2)
    runtime._bots[r2.id] = bot2
    dp2 = Dispatcher(storage=MemoryStorage())
    dp2.include_router(build_router(r2.id))
    await dp2.feed_update(bot2, msg(OWNER, "/admin"))
    await dp2.feed_update(bot2, tap(OWNER, button(session2, "Payments")))
    await dp2.feed_update(bot2, tap(OWNER, button(session2, "Connect 🪙 OxaPay")))
    await dp2.feed_update(bot2, msg(OWNER, gw.ox_key))
    r2 = await repo.get_reseller(r2.id)
    n_ox = len(gw.ox)

    async def typed2(uid, text):
        await dp2.feed_update(bot2, msg(uid, "/start"))
        await dp2.feed_update(bot2, tap(uid, button(session2, "Balance")))
        await dp2.feed_update(bot2, tap(uid, button(session2, "Add balance")))
        first = session2.last_text()
        await dp2.feed_update(bot2, tap(uid, button(session2, "Other amount")))
        prompt = session2.last_text()
        await dp2.feed_update(bot2, msg(uid, text))
        return first, prompt, session2.last_text()

    first, prompt, answer = await typed2(9940, "13,000")
    check("crypto only: from $1.00, the old amounts, no rupiah anywhere",
          payments.limits(r2) == (D("1"), D("1000")) and payments.presets(r2) == [D("5"), D("10"), D("20"), D("50")]
          and "You pay by crypto" in first and "Amounts are in US dollars ($)." in first and "Rp" not in first
          and "Minimum: <b>$1.00</b>" in first and "rupiah" not in prompt)
    check("'13,000' is not $13.00 here either: asked again, no link made",
          "Send an amount from <b>$1.00</b> to <b>$1000.00</b>" in answer and len(gw.ox) == n_ox, answer[:120])
    _, _, answer = await typed2(9940, "Rp 20.000")
    check("rupiah where no QRIS is taken: asked again", "Send an amount from <b>$1.00</b>" in answer and len(gw.ox) == n_ox)
    _, _, answer = await typed2(9940, "13,5")
    check("a decimal comma is a decimal: $13.50", len(gw.ox) == n_ox + 1 and "$13.50" in answer, answer[:120])

    # ── a deposit the owner approves by hand is claimed in dollars ──
    await repo.update_reseller(r2.id, deposit_info="Bank transfer, then send the receipt")
    for text, ok in (("13,000", False), ("Rp 20.000", False), ("2,50", True)):
        await dp2.feed_update(bot2, msg(9941, "/start"))
        await dp2.feed_update(bot2, tap(9941, "n:deposit_manual"))
        await dp2.feed_update(bot2, msg(9941, text))
        if ("transaction ID" in session2.last_text()) != ok:
            wrong.append((text, session2.last_text()[:60]))
    check("a manual deposit: '13,000' and rupiah are asked again, '2,50' is $2.50", not wrong, str(wrong))


async def test_proxy_data():
    """Proxy data packages in a shop. A shop owner asked why not everything is in the
    shop bot (2026-11-24). Unlike a number a package is paid for when it is bought."""
    print("proxy data packages")
    from aiogram.methods import SendMessage
    from app.bots.callbacks import Adm, Nav, Ord, Px
    from app.bots.reseller import build_router
    from app.models import Member
    from app.texts import order_card, proxy_card
    own = 5141
    api = FakeNumberHub()
    r = await make_reseller(api, owner=own, bot_id=86601)
    session = FakeSession(bot_id=86601)
    bot = fake_bot(session)
    runtime._bots[r.id] = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(r.id))
    cust = 94001
    m = await member(r, cust, "5.00")
    labels = lambda: [b.text for row in (session.last_markup().inline_keyboard if session.last_markup() else [])  # noqa: E731
                      for b in row]
    urls = lambda: [b.url for row in (session.last_markup().inline_keyboard if session.last_markup() else [])     # noqa: E731
                    for b in row if b.url]
    wallet0 = api.wallet

    async def state_of(mem):
        fresh = await repo.get_member(mem.id)
        return fresh.balance, fresh.held

    # ── the way in ──
    await dp.feed_update(bot, msg(cust, "/start"))
    check("the menu has a Proxy data button", button(session, "Proxy data") == Px(a="home").pack())
    await dp.feed_update(bot, tap(cust, button(session, "Proxy data")))
    check("the packages at the shop's price (the wallet's plus 30%, rounded up), the popular one marked",
          labels()[:3] == ["1 GB · $0.64", "⭐ 5 GB · $3.19", "10 GB · $6.37"] and "within 30 days" in session.last_text(),
          str(labels()))
    await dp.feed_update(bot, tap(cust, button(session, "5 GB")))
    check("the price is confirmed before anything is bought: paid now, per GB, no return",
          "<b>$3.19</b> ($0.64 per GB), paid now" in session.last_text() and "can't be returned" in session.last_text()
          and "Your balance: <b>$5.00</b>" in session.last_text()
          and button(session, "Buy · $3.19") == Px(a="buy", g=5, c=319).pack() and not api.proxy_bodies)

    # ── buying ──
    await dp.feed_update(bot, tap(cust, button(session, "Buy · $3.19")))
    o = (await repo.member_orders(m.id, 1))[0]
    check("bought: the customer is charged at once and nothing stays held",
          await state_of(m) == (D("1.81"), D("0")) and o.kind == "proxy" and o.status == "received"
          and o.settled == Order.CHARGED and o.country == "5" and o.nh_price == D("2.45"), str(await state_of(m)))
    check("one purchase at the wallet, for exactly this package at the wallet's price, paid there at once",
          api.proxy_bodies == [{"gb": 5, "max_price": "2.45"}] and api.wallet == wallet0 - D("2.45") and api.held == 0)
    text = session.last_text()
    check("the card says how to connect, the address in numbers, the ready lines and what is left",
          "Proxy data · 5 GB" in text and "Host: <code>203.0.113.7</code>" in text
          and "<code>8080</code> (HTTP) or <code>1080</code> (SOCKS5)" in text and f"Login: <code>{o.phone}</code>" in text
          and "Password: <code>Pw" in text and "-country-us:" in text and "-sid-" in text
          and "Used 0.00 GB of 5.00 GB, 5.00 GB left" in text and "Works until" in text and "days left)" in text
          and "Paid: $3.19" in text and "wallet.example" not in text, text[:400])
    check("one tap sets it up in Telegram, with the address in numbers",
          bool(urls()) and urls()[0].startswith(f"https://t.me/socks?server=203.0.113.7&port=1080&user={o.phone}&pass=Pw"),
          str(urls()))
    await dp.feed_update(bot, tap(cust, Nav(to="orders").pack()))
    check("My orders lists the package", any("🌐 5 GB · $3.19" in x for x in labels()), str(labels()))
    await dp.feed_update(bot, tap(cust, button(session, "5 GB")))
    check("…and opens its card again", "Host: <code>203.0.113.7</code>" in session.last_text())
    calls = len(api.calls)
    await selling.sync_reseller(r)
    check("the order sync never asks about a package (it would for 30 days)", len(api.calls) == calls)
    selling._proxy_live.clear()
    calls = len(api.calls)
    await dp.feed_update(bot, tap(cust, Ord(a="view", id=o.id).pack()))
    await dp.feed_update(bot, tap(cust, Ord(a="view", id=o.id).pack()))
    await dp.feed_update(bot, tap(cust, Ord(a="cancel", id=o.id).pack()))
    check("Refresh tapped again and again asks the wallet once; a package has nothing to cancel",
          len(api.calls) - calls == 1 and (await repo.get_order(o.id)).status == "received"
          and await state_of(m) == (D("1.81"), D("0")))
    other = await member(r, 94002, "9.00")
    shown, asked = len(session.texts()), len(api.calls)
    await dp.feed_update(bot, tap(94002, Ord(a="view", id=o.id).pack()))
    check("another customer cannot open it: told it is closed, nothing is shown, the wallet is not asked",
          "closed" in session.alerts()[-1] and len(session.texts()) == shown and len(api.calls) == asked)

    # ── what must not buy ──
    n = len(api.proxy_bodies)
    await dp.feed_update(bot, tap(cust, Px(a="buy", g=10, c=637).pack()))
    check("not enough balance: nothing bought, nothing held", "$6.37" in session.last_text()
          and len(api.proxy_bodies) == n and await state_of(m) == (D("1.81"), D("0")))
    await dp.feed_update(bot, tap(cust, Px(a="buy", g=1, c=1).pack()))
    check("a button made by hand with a lower price buys nothing: the real price to confirm",
          len(api.proxy_bodies) == n and button(session, "Buy · $0.64") is not None
          and "price just changed to <b>$0.64</b>" in session.last_text())
    await dp.feed_update(bot, tap(cust, Px(a="buy", g=999, c=1).pack()))
    check("a package that does not exist shows the list", len(api.proxy_bodies) == n and "Pick a package" in session.last_text())
    api.proxy_packages_list[0]["price"] = "0.60"
    await dp.feed_update(bot, tap(cust, Px(a="buy", g=1, c=64).pack()))
    check("the price moved since the screen: nothing bought, the new price to confirm",
          "price just changed to <b>$0.78</b>" in session.last_text() and button(session, "Buy · $0.78") is not None
          and len(api.proxy_bodies) == n)
    api.fail_next_buy = "price_exceeded"
    await dp.feed_update(bot, tap(cust, Px(a="buy", g=1, c=78).pack()))
    check("the wallet says the price is higher: nothing charged or held, asked to confirm again",
          "price just changed" in session.last_text() and await state_of(m) == (D("1.81"), D("0")))
    api.fail_next_buy = "purchase_failed"
    await dp.feed_update(bot, tap(cust, Px(a="buy", g=1, c=78).pack()))
    check("the wallet cannot sell one now: said plainly, nothing charged or held",
          "can't be bought right now" in session.last_text() and await state_of(m) == (D("1.81"), D("0")))
    api.fail_next_buy = "insufficient_funds"
    await dp.feed_update(bot, tap(cust, Px(a="buy", g=1, c=78).pack()))
    check("the owner's wallet is empty: the customer keeps their money",
          await state_of(m) == (D("1.81"), D("0")) and "Host:" not in session.last_text())
    n = len(api.proxy_bodies)
    await asyncio.gather(dp.feed_update(bot, tap(cust, Px(a="buy", g=1, c=78).pack())),
                         dp.feed_update(bot, tap(cust, Px(a="buy", g=1, c=78).pack())))
    check("a double tap buys one package", len(api.proxy_bodies) == n + 1 and await state_of(m) == (D("1.03"), D("0")),
          f"{len(api.proxy_bodies) - n} {await state_of(m)}")
    limit = selling.PROXY_MAX_ACTIVE
    selling.PROXY_MAX_ACTIVE = 2
    await dp.feed_update(bot, tap(cust, Px(a="buy", g=1, c=78).pack()))
    selling.PROXY_MAX_ACTIVE = limit
    check("a customer with the most packages that still work is told so, and nothing is bought",
          "already have 2 data packages" in session.last_text() and len(api.proxy_bodies) == n + 1
          and await state_of(m) == (D("1.03"), D("0")))

    # ── a reply that never came ──
    api.lose_replies = 3
    wallet1, n = api.wallet, len(api.orders)
    try:
        await selling.buy_proxy(r, other, 1, D("0.78"))
        check("three lost replies -> processing", False)
    except SellError as e:
        check("the wallet's answer is lost: 'processing', the customer's money held, not charged",
              e.reason == "processing" and await state_of(other) == (D("9.00"), D("0.78")))
    from app import repo as _r
    orig = _r.stale_buying
    _r.stale_buying = lambda older_than_sec=60: orig(older_than_sec=0)
    try:
        await selling.recover_buying()
        await selling.recover_buying()
    finally:
        _r.stale_buying = orig
    rec = (await repo.member_orders(other.id, 1))[0]
    sent = [x.text or "" for x in session.requests if isinstance(x, SendMessage) and x.chat_id == 94002]
    check("recovery asks again with the same request: ONE package, charged once, and the customer gets its card",
          len(api.orders) == n + 1 and api.wallet == wallet1 - D("0.60") and rec.status == "received"
          and rec.settled == Order.CHARGED and await state_of(other) == (D("8.22"), D("0"))
          and any("Proxy data · 1 GB" in x for x in sent), f"{len(api.orders) - n} {await state_of(other)}")

    # ── when the wallet cannot be read ──
    api.proxy_usage_down = True
    selling._proxy_live.clear()
    await dp.feed_update(bot, tap(cust, Ord(a="view", id=o.id).pack()))
    check("no usage figures: the card still says how to connect", "Host: <code>203.0.113.7</code>" in session.last_text()
          and "Used" not in session.last_text())
    api.proxy_usage_down, api.proxy_down = False, True
    selling._proxy_live.clear()
    await dp.feed_update(bot, tap(cust, Ord(a="view", id=o.id).pack()))
    check("the wallet does not answer: the card says so and offers to try again, nothing is lost",
          "can't be loaded right now" in session.last_text() and button(session, "Connection details") is not None
          and "Host:" not in session.last_text() and "Proxy data · 5 GB" in session.last_text())
    api.proxy_down = False

    # ── a package that ended ──
    api.orders[o.nh_id].update(status="completed", expires_at=fakes_iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)))
    selling._proxy_live.clear()
    await dp.feed_update(bot, tap(cust, Ord(a="view", id=o.id).pack()))
    check("ended: said so, no login is shown any more, and it no longer counts as working",
          "This package has ended" in session.last_text() and "Host:" not in session.last_text()
          and (await repo.get_order(o.id)).status == "completed" and await repo.proxy_active_count(m.id) == 1
          and button(session, "Proxy data") is not None)

    # ── the owner's switch ──
    await dp.feed_update(bot, msg(own, "/admin"))
    check("the owner's panel says it is on, with a switch",
          "🌐 Proxy data: <b>on</b>" in session.last_text() and button(session, "Proxy data: on") == Adm(a="proxy", arg="off").pack())
    await dp.feed_update(bot, tap(own, button(session, "Proxy data: on")))
    check("switched off", (await repo.get_reseller(r.id)).proxy_on == 0 and "🌐 Proxy data: <b>off</b>" in session.last_text()
          and button(session, "Proxy data: off") is not None)
    await dp.feed_update(bot, msg(cust, "/start"))
    n = len(api.proxy_bodies)
    gone = button(session, "Proxy data") is None
    await dp.feed_update(bot, tap(cust, Px(a="buy", g=1, c=78).pack()))
    check("off: the button is gone and an old button buys nothing", gone and len(api.proxy_bodies) == n)
    second = next(x for x in await repo.member_orders(m.id, 5) if x.kind == "proxy" and x.id != o.id)
    selling._proxy_live.clear()
    await dp.feed_update(bot, tap(cust, Ord(a="view", id=second.id).pack()))
    check("…and a package bought before still opens", "Host: <code>203.0.113.7</code>" in session.last_text())
    await dp.feed_update(bot, msg(own, "/admin"))
    await dp.feed_update(bot, tap(own, button(session, "Proxy data: off")))
    await dp.feed_update(bot, msg(cust, "/start"))
    check("switched on again: the button is back", button(session, "Proxy data") is not None)
    api2 = FakeNumberHub()
    api2.proxy_enabled = False
    r2 = await make_reseller(api2, owner=own, bot_id=86602)
    check("a wallet that sells no packages: no button, nothing to buy", await selling.proxy_packages(r2) == [])

    # ── every language ──
    keys = ["btn_proxy", "proxy_pick", "proxy_confirm", "btn_proxy_buy", "proxy_card_title", "proxy_active",
            "proxy_ended", "proxy_failed", "proxy_usage", "proxy_access", "proxy_card_closed", "btn_proxy_show",
            "btn_proxy_tg", "err_proxy_limit", "proxy_unavailable"]
    missing = [(lang, k) for lang in LANGS for k in keys if k not in _table(lang)]
    same = [(lang, k) for lang in LANGS if lang != "en" for k in keys if _table(lang).get(k) == _table("en")[k]]
    check("the proxy texts exist in all 14 languages, translated", not missing and not same, f"{missing[:3]} {same[:3]}")
    live = api.proxy_public(api.orders[second.nh_id], usage=True)
    errs = []
    now = dt.datetime.now(dt.timezone.utc)
    for lang in LANGS:
        mem = Member(id=1, reseller_id=1, telegram_id=1, language=lang, balance=D("1"), held=D("0"))
        for st in ("buying", "received", "completed", "failed"):
            row = Order(id=1, reseller_id=1, member_id=1, kind="proxy", service="proxy", country="5", phone="nh1x",
                        member_price=D("3.19"), status=st, settled=Order.CHARGED if st in ("received", "completed") else 0,
                        expires_at=now + dt.timedelta(days=29))
            for card in (proxy_card(mem, row, live), proxy_card(mem, row, None), order_card(mem, row)):
                if "{" in card[0] or not card[1].inline_keyboard:
                    errs.append(f"{lang}/{st}")
    check("the card renders cleanly in every language and state, with and without the wallet's answer", not errs, str(errs[:5]))
    runtime._bots.pop(r.id, None)


async def test_review_2026_11_24():
    """What the full review of 2026-11-24 found, each as a check."""
    print("fixes from the full review")
    from aiogram.methods import SendMessage
    from app import fx, payments
    from app.bots.callbacks import Adm, Em, Nav
    from app.bots.reseller import build_router, parse_amount
    from app.models import Payment
    from fakes import FakeFx, FakeGateways
    gw = FakeGateways()
    gw.next = 9000
    payments.TRANSPORT = gw.transport()
    fakefx = FakeFx()
    fx.TRANSPORT, fx._rate = fakefx.transport(), None
    await fx.refresh()
    own = 5151
    api = FakeNumberHub()
    api.email_guesses = [{"site": "shopee.com", "name": "Shopee"}]
    api.email_domains["shopee.com"] = [{"domain": "gmx.com", "price": "0.03", "in_stock": True, "delivery": None}]
    r = await make_reseller(api, owner=own, bot_id=86701)
    session = FakeSession(bot_id=86701)
    bot = fake_bot(session)
    runtime._bots[r.id] = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(r.id))
    cust = 95001
    m = await member(r, cust, "2.00")
    to = lambda uid: [x.text or "" for x in session.requests if isinstance(x, SendMessage) and x.chat_id == uid]  # noqa: E731
    await dp.feed_update(bot, msg(own, "/admin"))
    await dp.feed_update(bot, tap(own, button(session, "Payments")))
    await dp.feed_update(bot, tap(own, button(session, "Connect 🪙 OxaPay")))
    await dp.feed_update(bot, msg(own, gw.ox_key))
    await repo.update_reseller(r.id, deposit_info="Bank transfer, then send the receipt")
    r = await repo.get_reseller(r.id)

    # ── a form ends when its user does something else ──
    await dp.feed_update(bot, msg(cust, "/start"))
    await dp.feed_update(bot, tap(cust, Nav(to="deposit_manual").pack()))
    await dp.feed_update(bot, msg(cust, "5"))
    check("a manual deposit asks for its proof", "transaction ID" in session.last_text())
    await dp.feed_update(bot, msg(cust, "/buy"))
    await dp.feed_update(bot, msg(cust, "whatsapp"))
    check("a command ends the deposit: text typed after it is a search, not a proof sent to the owner",
          await repo.pending_deposit(m.id) is None and "Results for" in session.last_text(), session.last_text()[:80])
    n_links = len(gw.ox)
    await dp.feed_update(bot, tap(cust, Nav(to="deposit").pack()))
    await dp.feed_update(bot, tap(cust, button(session, "Other amount")))
    await dp.feed_update(bot, tap(cust, Ord(a="view", id=999999).pack()))
    await dp.feed_update(bot, msg(cust, "5"))
    check("a button outside the top-up ends it: '5' typed later opens no payment", len(gw.ox) == n_links)
    await dp.feed_update(bot, msg(cust, "/start"))
    await dp.feed_update(bot, tap(cust, button(session, "Email codes")))
    await dp.feed_update(bot, tap(cust, Nav(to="orders").pack()))
    await dp.feed_update(bot, msg(cust, "openai.com"))
    check("…and a site typed after leaving Email codes opens no mailbox list", "gmx.com" not in session.last_text())

    # ── amounts are read one way everywhere ──
    check("the owner's amounts: thousands and rupiah are not guessed, an exponent is not an amount",
          parse_amount("13.000") is None and parse_amount("13,000") is None and parse_amount("1e5") is None
          and parse_amount("Rp 5000") is None and parse_amount("2,5") == D("2.50") and parse_amount("$3") == D("3.00")
          and parse_amount("0.004") is None and parse_amount("1000") == D("1000.00"))
    check("digits apart are not joined, bank-style rupiah is read, coins that mean dollars are dollars",
          payments.read_amount("2 50") is None and payments.read_amount("Rp 50.000,00") == ("idr", D("50000.00"), True)
          and payments.read_amount("Rp13.000,-") == ("idr", D("13000"), True)
          and payments.read_amount("5 usdt") == ("usd", D("5"), False) and payments.read_amount("US$5") == ("usd", D("5"), False))
    for text, ok in (("5.000", False), ("2,500", False), ("13,000", False), ("$1,000", True), ("7", True)):
        await dp.feed_update(bot, msg(cust, "/start"))
        await dp.feed_update(bot, tap(cust, Nav(to="deposit_manual").pack()))
        await dp.feed_update(bot, msg(cust, text))
        if ("transaction ID" in session.last_text()) != ok:
            check(f"a manual deposit of {text!r}", False, session.last_text()[:60])
    check("a manual deposit written in thousands is asked again, never claimed as that many dollars",
          await repo.pending_deposit(m.id) is None)
    await dp.feed_update(bot, msg(cust, "/start"))
    await dp.feed_update(bot, tap(cust, Nav(to="deposit").pack()))
    await dp.feed_update(bot, tap(cust, button(session, "Other amount")))
    n_links = len(gw.ox)
    await dp.feed_update(bot, msg(cust, "1.000"))
    check("'1.000' where only dollars are taken is asked again: no $1000 link, no $1 link",
          len(gw.ox) == n_links and "Send an amount from" in session.last_text())

    # ── the daily limit counts what was opened and not paid ──
    before = await repo.payments_today(m.id)
    p = await repo.create_payment(r.id, m.id, payments.OXAPAY, D("1"))
    refused = await repo.payments_today(m.id)
    await repo.attach_invoice(p.id, "inv-rev-1", "https://pay.example/1", dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1))
    opened = await repo.payments_today(m.id)
    check("a payment the gateway refused does not count against the customer's day; one that was opened does",
          refused == before and opened == before + 1, f"{before} {refused} {opened}")

    # ── a payment that can never be confirmed is closed, not asked for ever ──
    late = await repo.create_payment(r.id, m.id, payments.OXAPAY, D("5"))
    await repo.attach_invoice(late.id, "no-such-invoice", "https://pay.example/x",
                              dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=7))
    fresh = await repo.create_payment(r.id, m.id, payments.OXAPAY, D("5"))
    await repo.attach_invoice(fresh.id, "no-such-invoice-2", "https://pay.example/y",
                              dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30))
    s1 = await payments.check(await repo.get_payment(late.id), r)
    s2 = await payments.check(await repo.get_payment(fresh.id), r)
    check("the gateway cannot be asked about it: long past its end it is closed, inside its time it stays open",
          s1 == "expired" and (await repo.get_payment(late.id)).status == Payment.EXPIRED
          and s2 == "unknown" and (await repo.get_payment(fresh.id)).status == Payment.PENDING, f"{s1} {s2}")

    # ── the owner's minimum cannot refuse every top-up ──
    await dp.feed_update(bot, msg(own, "/admin"))
    await dp.feed_update(bot, tap(own, button(session, "Payments")))
    await dp.feed_update(bot, tap(own, button(session, "Minimum")))
    await dp.feed_update(bot, msg(own, "10000"))
    check("a minimum above what one top-up can be is refused with the reason",
          "at most <b>$1000.00</b>" in session.last_text() and (await repo.get_reseller(r.id)).deposit_min is None,
          session.last_text()[:120])
    await dp.feed_update(bot, msg(own, "13.000"))
    check("…and one written in thousands is asked again", "in US dollars" in session.last_text()
          and (await repo.get_reseller(r.id)).deposit_min is None)
    await dp.feed_update(bot, msg(own, "/admin"))
    await repo.update_reseller(r.id, deposit_min=D("5000"))
    check("a minimum left over from before is not applied when it would refuse everything",
          payments.limits(await repo.get_reseller(r.id)) == (D("1"), D("1000")))
    await repo.update_reseller(r.id, deposit_min=None)

    # ── a broadcast is shown first ──
    await member(r, 95002, "0")
    await dp.feed_update(bot, msg(own, "/admin"))
    await dp.feed_update(bot, tap(own, Adm(a="broadcast").pack()))
    await dp.feed_update(bot, msg(own, "First draft"))
    first_button = button(session, "Send to")
    check("the owner sees the message and how many get it; nothing is sent yet",
          "Your customers will get this message" in session.last_text() and "First draft" in session.last_text()
          and "Nothing has been sent yet" in session.last_text() and first_button is not None
          and button(session, "Don't send") is not None and not any("First draft" in x for x in to(95002)))
    await dp.feed_update(bot, tap(own, Adm(a="broadcast").pack()))
    await dp.feed_update(bot, msg(own, "Second draft"))
    await dp.feed_update(bot, tap(own, first_button))
    await asyncio.sleep(0.3)
    check("the Send button of an older draft sends nothing", "no longer waiting" in session.alerts()[-1]
          and not any("draft" in x for x in to(95002)))
    await dp.feed_update(bot, tap(own, button(session, "Send to")))
    await asyncio.sleep(0.5)
    check("the Send button of the draft on screen sends exactly that one, once",
          [x for x in to(95002) if "draft" in x] == ["Second draft"], str(to(95002)))
    await dp.feed_update(bot, tap(own, Adm(a="bc_send", arg="00000000").pack()))
    await asyncio.sleep(0.3)
    check("…and tapping Send again sends nothing more", [x for x in to(95002) if "draft" in x] == ["Second draft"])

    # ── email: an old button cannot buy another site's mailbox; a guessed address is shown ──
    await dp.feed_update(bot, msg(cust, "/start"))
    await dp.feed_update(bot, tap(cust, button(session, "Email codes")))
    await dp.feed_update(bot, tap(cust, button(session, "OpenAI")))
    await dp.feed_update(bot, tap(cust, button(session, "gmx.com")))
    old_buy = button(session, "Get the address")
    await dp.feed_update(bot, tap(cust, Em(a="home").pack()))
    await dp.feed_update(bot, tap(cust, button(session, "Google")))
    n_mail = len(api.email_bodies)
    await dp.feed_update(bot, tap(cust, old_buy))
    check("a 'Get the address' button from another site's screen buys nothing: the picker again",
          len(api.email_bodies) == n_mail and "Type the website's address" in session.last_text())
    await dp.feed_update(bot, tap(cust, Em(a="buy", i=0, c=99).pack()))
    check("…and neither does a button made by hand", len(api.email_bodies) == n_mail)
    await dp.feed_update(bot, msg(cust, "shopee"))
    check("a site found by name whose address is a guess shows the address on its button",
          button(session, "Shopee · shopee.com") is not None, str(session.last_text())[:60])
    await dp.feed_update(bot, tap(cust, button(session, "Shopee")))
    check("…and asks to check it before a mailbox is opened", "<b>shopee.com</b>" in session.last_text()
          and "Check the spelling" in session.last_text())
    await dp.feed_update(bot, tap(cust, button(session, "gmx.com")))
    api.fail_next_buy = "sold_out"
    await dp.feed_update(bot, tap(cust, button(session, "Get the address")))
    check("a mailbox that cannot be had is not called 'no numbers for this country'",
          "No mailbox is available" in session.last_text() and "numbers" not in session.last_text().lower())

    # ── a mail that could not be read once is asked for again, not announced empty ──
    await dp.feed_update(bot, tap(cust, button(session, "Back")))
    await dp.feed_update(bot, tap(cust, button(session, "gmx.com")))
    await dp.feed_update(bot, tap(cust, button(session, "Get the address")))
    box = (await repo.member_orders(m.id, 1))[0]
    api.mail_arrives(box.nh_id, link="https://shopee.example/confirm?t=abc")
    real, asked = selling.mail_details, []

    async def flaky(shop, order):
        asked.append(order.id)
        return {} if len(asked) == 1 else await real(shop, order)
    selling.mail_details = flaky
    try:
        await selling.sync_reseller(r)
        first_pass = [x for x in to(cust) if "shopee.example" in x or "sent a mail" in x]
        await selling.sync_reseller(r)
    finally:
        selling.mail_details = real
    check("the mail's details could not be read: nothing is announced yet; next pass the link is delivered",
          not first_pass and (await repo.get_order(box.id)).codes_announced == 1
          and any("https://shopee.example/confirm?t=abc" in x for x in to(cust)), str(first_pass))

    # ── one order that cannot be recovered does not stop the others ──
    api_b = FakeNumberHub()
    shop_b = await make_reseller(api_b, owner=own, bot_id=86702)
    mb = await member(shop_b, 95003, "3.00")
    values = dict(kind="sms", service="wa", service_name="WhatsApp", country="187", country_name="USA", country_iso="US",
                  member_price=D("0.39"), markup_pct_at_buy=D("30"), ceiling_at_buy=D("0.30"))
    stuck_a = await repo.create_order_with_hold(reseller_id=r.id, member_id=m.id, **values)
    stuck_b = await repo.create_order_with_hold(reseller_id=shop_b.id, member_id=mb.id, **values)
    from app import repo as _r
    orig_stale, orig_client = _r.stale_buying, selling.client_for

    def broken(rid):
        if rid == r.id:
            raise RuntimeError("this shop's client is broken")
        return orig_client(rid)
    _r.stale_buying = lambda older_than_sec=60: orig_stale(older_than_sec=0)
    selling.client_for = broken
    try:
        await selling.recover_buying()
    finally:
        _r.stale_buying, selling.client_for = orig_stale, orig_client
    check("recovery crashes on one shop's order: the next shop's order is still finished",
          (await repo.get_order(stuck_a.id)).status == Order.BUYING and (await repo.get_order(stuck_b.id)).status == "waiting")

    # ── a package with no clear answer for 20 hours: money back, and the owner is told ──
    lost = await repo.create_order_with_hold(reseller_id=shop_b.id, member_id=mb.id, kind="proxy", service="proxy",
                                             service_name="1 GB", country="1", country_name="1 GB", country_iso=None,
                                             member_price=D("0.64"), markup_pct_at_buy=D("30"), ceiling_at_buy=D("0.49"))
    session_b = FakeSession(bot_id=86702)
    runtime._bots[shop_b.id] = fake_bot(session_b)
    give_up = selling.BUYING_GIVE_UP_SEC
    _r.stale_buying = lambda older_than_sec=60: orig_stale(older_than_sec=0)
    selling.BUYING_GIVE_UP_SEC = -1
    try:
        await selling.recover_buying()
        await asyncio.sleep(0.2)
    finally:
        _r.stale_buying, selling.BUYING_GIVE_UP_SEC = orig_stale, give_up
    told = [x.text or "" for x in session_b.requests if isinstance(x, SendMessage) and x.chat_id == own]
    held = (await repo.get_member(mb.id)).held
    check("the customer's money is given back and the owner is told to look for the package",
          (await repo.get_order(lost.id)).status == Order.FAILED and held == D("0.39")
          and any("no clear answer" in x for x in told), f"{held} {told[-1:]}")
    await repo.fail_order(stuck_a.id, "test")
    runtime._bots.pop(r.id, None)
    runtime._bots.pop(shop_b.id, None)


async def test_number_cycling():
    """One customer taking number after number that never gets a code costs them
    nothing, and gets the owner's whole wallet account paused for every shop."""
    print("a customer who cycles numbers is paused for the day")
    from aiogram.methods import SendMessage
    from app.bots.callbacks import Buy
    from app.bots.reseller import build_router
    from app.db import session_factory
    own = 5161
    api = FakeNumberHub()
    r = await make_reseller(api, owner=own, bot_id=86801)
    session = FakeSession(bot_id=86801)
    bot = fake_bot(session)
    runtime._bots[r.id] = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(r.id))
    m = await member(r, 96001, "5.00")
    good = await member(r, 96002, "5.00")
    check("the rule: 30 with no code at all, or 100 with under 3%; an honest heavy day is not cycling",
          selling.cycling(30, 0) and not selling.cycling(29, 0) and not selling.cycling(66, 6)
          and not selling.cycling(39, 1) and selling.cycling(98, 2) and not selling.cycling(97, 3)
          and not selling.cycling(500, 16) and not selling.cycling(0, 0))

    async def past(mem, n, settled, hours=2):
        """n finished numbers of this customer, bought `hours` ago."""
        when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
        async with session_factory() as s:
            for _ in range(n):
                s.add(Order(reseller_id=r.id, member_id=mem.id, kind="sms", service="wa", service_name="WhatsApp",
                            country="187", country_name="USA", country_iso="US", member_price=D("0.39"),
                            markup_pct_at_buy=D("30"), ceiling_at_buy=D("0.30"), codes="[]",
                            status="completed" if settled == Order.CHARGED else "canceled", settled=settled,
                            created_at=when, updated_at=when))
            await s.commit()

    await past(m, 29, Order.RELEASED)
    o = await selling.buy(r, m, "wa", "187", D("0.39"))
    check("29 numbers with no code today: the next one is still sold", o.status == "waiting")
    await selling.cancel(r, await repo.get_member(m.id), o.id)       # the fake has no cancel lock: it ends with no code
    check("…and it ends with no code too: 30 now", await repo.number_outcomes(m.id) == (30, 0),
          str(await repo.number_outcomes(m.id)))
    n = len(api.orders)
    try:
        await selling.buy(r, m, "wa", "187", D("0.39"))
        check("the 31st is refused", False)
    except SellError as e:
        check("the next number is refused before anything is asked of the wallet or held",
              e.reason == "number_limit" and len(api.orders) == n and (await repo.get_member(m.id)).held == 0)
    await asyncio.sleep(0.2)
    told = [x.text or "" for x in session.requests if isinstance(x, SendMessage) and x.chat_id == own]
    check("the owner is told once, with the customer's ID and the figures",
          len([x for x in told if "paused for one customer" in x]) == 1 and "<code>96001</code>" in told[-1]
          and "took 30 numbers in a day and 0 got a code" in told[-1], str(told[-1:])[:160])
    await dp.feed_update(bot, tap(96001, Buy(code="wa", cc="187", cents=39).pack()))
    await asyncio.sleep(0.2)
    told = [x.text or "" for x in session.requests if isinstance(x, SendMessage) and x.chat_id == own]
    check("in the bot the customer is told plainly, and the owner is not told again",
          "paused for you until tomorrow" in session.last_text() and "Nothing was charged" in session.last_text()
          and len([x for x in told if "paused for one customer" in x]) == 1, session.last_text()[:120])
    box = await selling.buy_email(r, m, "openai.com", "OpenAI", "gmx.com", D("0.04"))
    check("only numbers are paused: an email code can still be bought", box.kind == "email" and box.status == "waiting")
    o2 = await selling.buy(r, good, "wa", "187", D("0.39"))
    check("another customer of the same shop buys as usual", o2.status == "waiting")
    await past(good, 40, Order.RELEASED)
    await past(good, 1, Order.CHARGED)
    o3 = await selling.buy(r, good, "tg", "187", None)
    check("40 numbers and one code is a bad day, not cycling: still sold", o3.status == "waiting")
    old = await member(r, 96003, "5.00")
    await past(old, 60, Order.RELEASED, hours=30)
    o4 = await selling.buy(r, old, "wa", "187", D("0.39"))
    check("numbers from more than a day ago do not count: it lifts by itself", o4.status == "waiting")
    missing = [lang for lang in LANGS if "err_number_limit" not in _table(lang)]
    same = [lang for lang in LANGS if lang != "en" and _table(lang).get("err_number_limit") == _table("en")["err_number_limit"]]
    check("the customer's text exists in all 14 languages, translated", not missing and not same, f"{missing} {same}")
    runtime._bots.pop(r.id, None)


async def test_back_to_the_purchase():
    """After money is added, one tap brings the customer back to the number they were
    about to buy. The same notice used to end with no button at all."""
    print("after a top-up: back to the number being bought")
    from aiogram.methods import SendMessage
    from app import payments
    from app.bots.callbacks import Cty, Nav
    from app.bots.reseller import build_router
    from fakes import FakeGateways
    gw = FakeGateways()
    gw.next = 11000
    payments.TRANSPORT = gw.transport()
    own = 5171
    api = FakeNumberHub()
    r = await make_reseller(api, owner=own, bot_id=86901)
    session = FakeSession(bot_id=86901)
    bot = fake_bot(session)
    runtime._bots[r.id] = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(r.id))
    await dp.feed_update(bot, msg(own, "/admin"))
    await dp.feed_update(bot, tap(own, button(session, "Payments")))
    await dp.feed_update(bot, tap(own, button(session, "Connect 🪙 OxaPay")))
    await dp.feed_update(bot, msg(own, gw.ox_key))
    cust = 97001
    m = await member(r, cust, "0")

    def last_kb(uid):
        sent = [x for x in session.requests if isinstance(x, SendMessage) and x.chat_id == uid and x.reply_markup]
        return [(b.text, b.callback_data) for row in sent[-1].reply_markup.inline_keyboard for b in row] if sent else []

    # with nothing remembered: to the shop
    await repo.member_adjust(r.id, m.id, D("0.10"))
    await dp.feed_update(bot, msg(own, "/admin"))
    await dp.feed_update(bot, tap(own, Adm(a="add").pack()))
    await dp.feed_update(bot, msg(own, f"{cust} 0.10"))
    check("money added with nothing in mind: the notice leads to the shop",
          [x[1] for x in last_kb(cust)] == [Nav(to="buy").pack(), Nav(to="menu").pack()], str(last_kb(cust)))

    # the customer looks at a number they cannot afford, then tops up
    await dp.feed_update(bot, tap(cust, Cty(code="wa", cc="187").pack()))
    check("a number the balance does not cover is remembered",
          selling.remembered_route(m.id) is not None and selling.remembered_route(m.id)[:2] == ("wa", "187"))
    await dp.feed_update(bot, tap(cust, Nav(to="deposit").pack()))
    await dp.feed_update(bot, tap(cust, button(session, "$5.00")))
    pay = (await repo.open_payments(m.id))[0]
    gw.ox[pay.invoice_id]["status"] = "Paid"
    await payments.poll_all()
    kb = last_kb(cust)
    check("payment received: the first button continues with that number, by name and flag",
          (await repo.get_member(m.id)).balance == D("5.20") and kb and kb[0][1] == Cty(code="wa", cc="187").pack()
          and kb[0][0].startswith("▶️ Continue: WhatsApp") and kb[-1][1] == Nav(to="menu").pack(), str(kb))
    await dp.feed_update(bot, tap(cust, kb[0][1]))
    check("one tap shows the number again, ready to buy", button(session, "Buy") is not None
          and "$5.20" in session.last_text(), session.last_text()[:120])
    check("…and with enough balance it is still kept until the number is bought",
          selling.remembered_route(m.id) is not None)
    await dp.feed_update(bot, tap(cust, button(session, "Buy")))
    check("bought: nothing is kept any more", selling.remembered_route(m.id) is None
          and (await repo.member_orders(m.id, 1))[0].status == "waiting")
    await dp.feed_update(bot, msg(own, "/admin"))
    await dp.feed_update(bot, tap(own, Adm(a="add").pack()))
    await dp.feed_update(bot, msg(own, f"{cust} 1"))
    check("…so the next 'money added' leads to the shop again", [x[1] for x in last_kb(cust)][0] == Nav(to="buy").pack())
    hit = selling._resume
    selling.remember_route(m.id, "tg", "187", "Telegram", "🇺🇸")
    hit[m.id] = (hit[m.id][0] - selling.RESUME_SEC - 5,) + hit[m.id][1:]
    check("a number looked at more than a day ago is forgotten", selling.remembered_route(m.id) is None)
    missing = [lang for lang in LANGS if "btn_continue" not in _table(lang)]
    same = [lang for lang in LANGS if lang != "en" and _table(lang).get("btn_continue") == _table("en")["btn_continue"]]
    check("the button exists in all 14 languages, translated", not missing and not same, f"{missing} {same}")
    runtime._bots.pop(r.id, None)


async def test_rentals():
    """Rented numbers in a shop (a shop owner asked on 2026-11-24 why not everything is in
    the shop bot). The owner's wallet pays when a number is rented. The customer's price
    is held with the order and charged once the number can no longer be given back: at
    its first SMS, or when the wallet's refund time is over."""
    print("rented numbers")
    import json
    from aiogram.methods import SendMessage
    from app.bots.callbacks import Adm, Nav, Ord, Rn
    from app.bots.reseller import build_router
    from app.models import Member
    from app.texts import order_card, rental_ended, rental_refunded, rental_sms
    own = 5151
    api = FakeNumberHub()
    r = await make_reseller(api, owner=own, bot_id=88801)
    session = FakeSession(bot_id=88801)
    bot = fake_bot(session)
    runtime._bots[r.id] = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(r.id))
    cust = 95001
    m = await member(r, cust, "10.00")
    labels = lambda: [b.text for row in (session.last_markup().inline_keyboard if session.last_markup() else [])  # noqa: E731
                      for b in row]
    sent_to = lambda uid: [x.text or "" for x in session.requests if isinstance(x, SendMessage) and x.chat_id == uid]  # noqa: E731
    wallet0 = api.wallet
    slow, confirm = selling.RENT_SYNC_SEC, selling.RENT_CANCEL_CONFIRM_SEC
    selling.RENT_SYNC_SEC = 0

    async def state_of(mem):
        fresh = await repo.get_member(mem.id)
        return fresh.balance, fresh.held

    def rent(code, cents, h=24, cc="187"):
        return tap(cust, Rn(a="buy", h=h, cc=cc, s=code, c=cents).pack())

    # ── the way in ──
    await dp.feed_update(bot, msg(cust, "/start"))
    check("the menu has a Rent a number button", button(session, "Rent a number") == Rn(a="home").pack())
    await dp.feed_update(bot, tap(cust, button(session, "Rent a number")))
    check("the periods the wallet offers, in the customer's words",
          labels()[:4] == ["1 day", "3 days", "1 week", "1 month"] and "For how long?" in session.last_text(), str(labels()))
    await dp.feed_update(bot, tap(cust, button(session, "1 day")))
    check("then the countries of that period",
          labels()[:2] == ["🇬🇧 United Kingdom", "🇺🇸 USA"] and "Rent for 1 day" in session.last_text(), str(labels()))
    await dp.feed_update(bot, tap(cust, button(session, "USA")))
    check("then what the number is for, at the shop's price: every SMS first, any other, the popular app, the rest; "
          "what is sold out is not listed",
          labels()[:4] == ["📨 Any service (all SMS) · $2.60", "❓ Any other app · $1.30", "WhatsApp · $1.95", "Zeta · $1.04"]
          and not any("Telegram" in x for x in labels()) and "USA · 1 day" in session.last_text(), str(labels()))
    await dp.feed_update(bot, tap(cust, button(session, "Any service")))
    text = session.last_text()
    check("the price is confirmed before anything is rented: paid now, the period, and when a refund is possible",
          "Price: <b>$2.60</b>, paid now" in text and "Period: <b>1 day</b>" in text and "20th minute" in text
          and "Your balance: <b>$10.00</b>" in text and not api.rent_bodies
          and button(session, "Rent · $2.60") == Rn(a="buy", h=24, cc="187", s="full", c=260).pack(), text[:300])

    # ── renting ──
    await dp.feed_update(bot, tap(cust, button(session, "Rent · $2.60")))
    o = (await repo.member_orders(m.id, 1))[0]
    check("rented: the price is held, not yet charged, and the order knows its period and its refund time",
          await state_of(m) == (D("10.00"), D("2.60")) and o.kind == "rent" and o.status == "waiting"
          and o.settled == Order.UNSETTLED and o.rent_hours == 24 and o.nh_price == D("2.00")
          and o.refund_until is not None and o.cancel_available_at is not None, f"{await state_of(m)} {o.status}")
    check("one purchase at the wallet for exactly this, paid there at once",
          api.rent_bodies == [{"h": 24, "country": "187", "code": "full", "max_price": "2.00"}]
          and api.wallet == wallet0 - D("2.00") and api.held == 0, str(api.rent_bodies))
    text = session.last_text()
    check("the card: the number, until when it is theirs, when a refund opens, and where its SMS will show",
          "Rented number" in text and "Any service (all SMS)" in text and f"+{o.phone}" in text
          and "Yours until" in text and "(24 hours left)" in text and "A refund of $2.60 opens in" in text
          and "Every SMS sent to it appears here" in text and "Paid: $2.60" in text
          and button(session, "Cancel in") == Ord(a="cancel", id=o.id).pack(), text[:400])
    await dp.feed_update(bot, tap(cust, Ord(a="cancel", id=o.id).pack()))
    check("a cancel in the first two minutes: told when a refund opens, nothing changes",
          "2 minutes after you rent" in session.alerts()[-1] and await state_of(m) == (D("10.00"), D("2.60"))
          and (await repo.get_order(o.id)).status == "waiting")

    # ── giving it back inside the refund time ──
    api.rental_age(o.nh_id, 5)
    await selling.sync_reseller(r)
    check("inside the refund time the card offers the refund and counts down; still nothing is charged",
          "get $2.60 back for another" in session.last_text()
          and button(session, "Cancel and refund") == Ord(a="cancel", id=o.id).pack()
          and (await repo.get_order(o.id)).settled == Order.UNSETTLED, session.last_text()[:300])
    api.rent_refuse_cancel = True
    await dp.feed_update(bot, tap(cust, Ord(a="cancel", id=o.id).pack()))
    check("the wallet could not cancel it: said so, the rental runs on, the money stays held",
          "did not go through" in session.alerts()[-1] and await state_of(m) == (D("10.00"), D("2.60"))
          and (await repo.get_order(o.id)).status == "waiting" and api.wallet == wallet0 - D("2.00"))
    api.rent_refuse_cancel = False
    await dp.feed_update(bot, tap(cust, Ord(a="cancel", id=o.id).pack()))
    o = await repo.get_order(o.id)
    check("given back inside the refund time: the customer has all of it again, and so has the wallet",
          "$2.60 is back on your balance" in session.alerts()[-1] and await state_of(m) == (D("10.00"), D("0"))
          and o.status == "canceled" and o.settled == Order.RELEASED and api.wallet == wallet0
          and "Cancelled — $2.60 returned" in session.last_text(), f"{await state_of(m)} {o.status} {session.alerts()[-1]}")
    check("a cancelled rental's card has nothing left to cancel", button(session, "Cancel") is None)
    await dp.feed_update(bot, tap(cust, Ord(a="cancel", id=o.id).pack()))
    check("its old cancel button says it is closed and changes nothing",
          "already closed" in session.alerts()[-1] and await state_of(m) == (D("10.00"), D("0")) and api.wallet == wallet0)

    # ── an SMS arrives: delivered in full, and the rental is charged ──
    await dp.feed_update(bot, rent("wa", 195))
    o2 = (await repo.member_orders(m.id, 1))[0]
    n_sent = len(sent_to(cust))
    api.rental_sms(o2.nh_id, "Your parcel is on its way.", "Parcel")
    await selling.sync_reseller(r)
    o2 = await repo.get_order(o2.id)
    new = sent_to(cust)[n_sent:]
    check("an SMS with no code in it reaches the customer in full",
          any("New SMS" in x and f"+{o2.phone}" in x and "<b>Parcel</b>: Your parcel is on its way." in x for x in new), str(new))
    check("from its first SMS the rental is charged, once; it keeps running",
          o2.settled == Order.CHARGED and o2.status == "waiting" and await state_of(m) == (D("8.05"), D("0"))
          and o2.codes_announced == 1, f"{o2.settled} {await state_of(m)}")
    card = order_card(await repo.get_member(m.id), o2)
    check("the card lists the SMS and no longer offers a refund",
          "📩 <b>Parcel</b>: Your parcel is on its way." in card[0] and "opens in" not in card[0]
          and not any("Cancel" in b.text for row in card[1].inline_keyboard for b in row), card[0][:300])
    api.rental_sms(o2.nh_id, "WhatsApp code 123-456", "", "123456")
    await selling.sync_reseller(r)
    await selling.sync_reseller(r)
    new = sent_to(cust)[n_sent:]
    check("the next SMS is sent once, with its code to copy; nothing more is charged",
          sum("WhatsApp code 123-456" in x for x in new) == 1 and any("<code>123456</code>" in x for x in new)
          and (await repo.get_order(o2.id)).codes_announced == 2 and await state_of(m) == (D("8.05"), D("0")), str(new))
    await dp.feed_update(bot, tap(cust, Ord(a="cancel", id=o2.id).pack()))
    check("an old cancel button after an SMS: no refund, said why",
          "can no longer be refunded" in session.alerts()[-1] and await state_of(m) == (D("8.05"), D("0")))

    # ── no SMS and the refund time passes ──
    await dp.feed_update(bot, rent("ot", 130, h=72))
    o3 = (await repo.member_orders(m.id, 1))[0]
    await selling.settle_sweep()
    check("a rental in its refund time is left alone by the sweep: held, not charged",
          await state_of(m) == (D("8.05"), D("1.30")) and (await repo.get_order(o3.id)).settled == Order.UNSETTLED
          and o3.rent_hours == 72 and api.rent_bodies[-1]["h"] == 72)
    api.rental_age(o3.nh_id, 25)
    await selling.sync_reseller(r)
    o3 = await repo.get_order(o3.id)
    check("the wallet no longer offers a refund: charged, as the wallet was from the start",
          o3.settled == Order.CHARGED and o3.status == "waiting" and o3.refund_until is None
          and await state_of(m) == (D("6.75"), D("0")), f"{o3.settled} {await state_of(m)}")
    card = order_card(await repo.get_member(m.id), o3)
    check("...and its card no longer offers one",
          "back for another" not in card[0] and not any("Cancel" in b.text for row in card[1].inline_keyboard for b in row))
    await dp.feed_update(bot, tap(cust, Ord(a="cancel", id=o3.id).pack()))
    check("an old cancel button after the refund time: refused, nothing changes",
          "time for a refund is over" in session.alerts()[-1] and await state_of(m) == (D("6.75"), D("0"))
          and (await repo.get_order(o3.id)).status == "waiting")

    # ── a shop with nothing but running rentals ──
    selling.RENT_SYNC_SEC = 15
    selling._rent_synced.pop(r.id, None)
    calls = len(api.calls)
    for _ in range(3):
        await selling.sync_reseller(r)
    check("it asks the wallet for news once in a while, not on every pass (a rental runs for days)",
          len(api.calls) - calls == 1, str(len(api.calls) - calls))
    selling.RENT_SYNC_SEC = 0

    # ── a cancel that shows in the list ──
    await dp.feed_update(bot, rent("zz", 104))
    o4 = (await repo.member_orders(m.id, 1))[0]
    api.rental_age(o4.nh_id, 5)
    api.orders[o4.nh_id]["status"] = "canceled"          # the wallet marks it while it asks the number's source...
    await selling.sync_reseller(r)
    await selling.settle_sweep()
    check("a rental the list shows cancelled for a moment is not refunded on the spot",
          (await repo.get_order(o4.id)).status == "waiting" and await state_of(m) == (D("6.75"), D("1.04")))
    api.orders[o4.nh_id]["status"] = "waiting"           # ...and puts it back: the source refused
    await selling.sync_reseller(r)
    check("when the wallet puts it back it simply runs on, the money still held",
          (await repo.get_order(o4.id)).status == "waiting" and (await repo.get_order(o4.id)).settled == Order.UNSETTLED
          and o4.id not in selling._rent_cancel_seen and await state_of(m) == (D("6.75"), D("1.04")))
    api.orders[o4.nh_id]["status"] = "canceled"          # a cancel made in the owner's own wallet app, refunded there
    api.wallet += D("0.80")
    await selling.sync_reseller(r)
    selling.RENT_CANCEL_CONFIRM_SEC = 0
    n_sent = len(sent_to(cust))
    await selling.sync_reseller(r)
    await selling.sync_reseller(r)
    selling.RENT_CANCEL_CONFIRM_SEC = confirm
    o4 = await repo.get_order(o4.id)
    check("a cancel that is still there later is believed: the customer is refunded once and told",
          o4.status == "canceled" and o4.settled == Order.RELEASED and await state_of(m) == (D("6.75"), D("0"))
          and sum("is cancelled. $1.04 is back" in x for x in sent_to(cust)[n_sent:]) == 1, f"{o4.status} {await state_of(m)}")

    # ── the period ends ──
    api.set_status(o2.nh_id, "completed")
    n_sent = len(sent_to(cust))
    await selling.sync_reseller(r)
    await selling.sync_reseller(r)
    o2 = await repo.get_order(o2.id)
    check("when the period ends the customer is told once and the card says so; nothing more is charged",
          sum("has ended" in x for x in sent_to(cust)[n_sent:]) == 1 and o2.status == "completed"
          and "This rental has ended" in order_card(m, o2)[0] and await state_of(m) == (D("6.75"), D("0")))
    check("a running rental does not use up the open orders a customer may have",
          await repo.member_open_count(m.id) == 0 and await repo.rent_active_count(m.id) == 1)
    await dp.feed_update(bot, tap(cust, Nav(to="orders").pack()))
    check("My orders lists the rented numbers with their numbers",
          sum("📅" in x and "+1777000" in x for x in labels()) >= 3, str(labels()))

    # ── what must not rent ──
    poor = await member(r, 95002, "1.00")
    n = len(api.rent_bodies)
    await dp.feed_update(bot, tap(95002, Rn(a="buy", h=24, cc="187", s="full", c=260).pack()))
    check("not enough balance: nothing rented, nothing held", "$2.60" in session.last_text()
          and len(api.rent_bodies) == n and await state_of(poor) == (D("1.00"), D("0")))
    await dp.feed_update(bot, rent("full", 1))
    check("a button made by hand with a lower price rents nothing: the real price to confirm",
          len(api.rent_bodies) == n and "price just changed to <b>$2.60</b>" in session.last_text()
          and button(session, "Rent · $2.60") is not None)
    await dp.feed_update(bot, rent("tg", 156))
    check("what is sold out is not rented: said so, next to the list of what is",
          "can't be rented right now" in session.alerts()[-1] and len(api.rent_bodies) == n
          and "What is the number for?" in session.last_text())
    await dp.feed_update(bot, rent("full", 260, h=48))
    check("a period the wallet does not offer shows the periods", "For how long?" in session.last_text()
          and len(api.rent_bodies) == n)
    api.fail_next_buy = "price_exceeded"
    await dp.feed_update(bot, rent("full", 260))
    check("the wallet says the price is higher: nothing held, and the price it named is the one to confirm",
          "price just changed to <b>$3.25</b>" in session.last_text() and button(session, "Rent · $3.25") is not None
          and await state_of(m) == (D("6.75"), D("0")), session.last_text()[:200])
    selling._rent_learned.clear()
    selling._rent_services.clear()
    api.rent_buy_down = True
    await dp.feed_update(bot, rent("full", 260))
    stuck = [x for x in await repo.stale_buying(older_than_sec=-5) if x.member_id == m.id]
    check("the wallet cannot price the rental at the purchase itself: nothing was bought, so nothing stays held or open",
          "Busy right now" in session.last_text() and await state_of(m) == (D("6.75"), D("0")) and not stuck,
          f"{session.last_text()[:80]} {await state_of(m)} {len(stuck)}")
    api.rent_buy_down, api.rent_down = False, True
    selling._rent_services.clear()
    await dp.feed_update(bot, rent("full", 260))
    check("...nor when the price list cannot be read: asked to try again", "Busy right now" in session.alerts()[-1]
          and await state_of(m) == (D("6.75"), D("0")))
    api.rent_down = False
    n = len(api.rent_bodies)
    await asyncio.gather(dp.feed_update(bot, rent("zz", 104)), dp.feed_update(bot, rent("zz", 104)))
    check("a double tap rents one number", len(api.rent_bodies) == n + 1 and await state_of(m) == (D("6.75"), D("1.04")),
          f"{len(api.rent_bodies) - n} {await state_of(m)}")
    limit = selling.RENT_MAX_ACTIVE
    selling.RENT_MAX_ACTIVE = 2
    await dp.feed_update(bot, rent("zz", 104))
    selling.RENT_MAX_ACTIVE = limit
    check("a customer renting the most numbers allowed is told so, and nothing is rented",
          "already renting 2 numbers" in session.last_text() and len(api.rent_bodies) == n + 1
          and await state_of(m) == (D("6.75"), D("1.04")), session.last_text()[:120])
    shown, asked = len(session.texts()), len(api.calls)
    await dp.feed_update(bot, tap(95002, Ord(a="cancel", id=o3.id).pack()))
    check("another customer can neither open nor cancel it: told it is closed, the wallet is not asked",
          "closed" in session.alerts()[-1] and len(session.texts()) == shown and len(api.calls) == asked
          and (await repo.get_order(o3.id)).status == "waiting")

    # ── a reply that never came ──
    other = await member(r, 95003, "9.00")
    api.lose_replies = 3
    wallet1, n_orders = api.wallet, len(api.orders)
    try:
        await selling.buy_rental(r, other, 168, "16", "full", D("3.90"))
        check("three lost replies -> processing", False)
    except SellError as e:
        check("the wallet's answer is lost: 'processing', the customer's money held",
              e.reason == "processing" and await state_of(other) == (D("9.00"), D("3.90")))
    from app import repo as _r
    orig = _r.stale_buying
    _r.stale_buying = lambda older_than_sec=60: orig(older_than_sec=0)
    try:
        await selling.recover_buying()
        await selling.recover_buying()
    finally:
        _r.stale_buying = orig
    rec = (await repo.member_orders(other.id, 1))[0]
    check("recovery asks again with the same request, the period included: ONE rental, still held, and the customer gets its card",
          len(api.orders) == n_orders + 1 and api.wallet == wallet1 - D("3.00") and rec.status == "waiting"
          and rec.settled == Order.UNSETTLED and rec.rent_hours == 168 and await state_of(other) == (D("9.00"), D("3.90"))
          and api.rent_bodies[-1] == {"h": 168, "country": "16", "code": "full", "max_price": "3.00"}
          and any("Rented number" in x and "(6 days left)" in x for x in sent_to(95003)),
          f"{len(api.orders) - n_orders} {rec.status} {await state_of(other)}")

    # ── a number nobody holds money for ──
    cli = api.client()
    lone = await cli.buy_rental(24, "187", "zz", D("0.80"), "orphan-rental-1")
    selling._orphans[int(lone["id"])] = (r.id, 0.0, "rent")
    await selling.release_orphans()
    check("asked back before its refund time opens: kept on the list, tried again later",
          int(lone["id"]) in selling._orphans and api.orders[lone["id"]]["status"] == "waiting")
    api.rental_age(lone["id"], 5)
    selling._orphans[int(lone["id"])] = (r.id, 0.0, "rent")
    before = api.wallet
    await selling.release_orphans()
    check("...and given back inside it: the wallet has its money again",
          int(lone["id"]) not in selling._orphans and api.orders[lone["id"]]["status"] == "canceled"
          and api.wallet == before + D("0.80"))
    late = await cli.buy_rental(24, "187", "zz", D("0.80"), "orphan-rental-2")
    api.rental_age(late["id"], 30)
    selling._orphans[int(late["id"])] = (r.id, 0.0, "rent")
    selling._owner_warned.clear()
    n_own = len(sent_to(own))
    await selling.release_orphans()
    check("too late to give back: dropped from the list, and the owner is told where to find it",
          int(late["id"]) not in selling._orphans and any("could not be given back" in x for x in sent_to(own)[n_own:]))
    box = await cli.buy_email("openai.com", "gmx.com", D("0.03"), "orphan-mailbox-1")
    selling._orphans[int(box["id"])] = (r.id, 0.0, "email")
    await selling.release_orphans()
    check("a mailbox nobody holds money for is cancelled as a mailbox (asked about as a number it stayed open)",
          int(box["id"]) not in selling._orphans and api.orders[box["id"]]["status"] == "canceled")
    await cli.close()

    # ── a new API key while a rental runs ──
    from app import provision as _prov
    from app.bots import builder as _bld
    only_rent = await make_reseller(api, owner=own + 1, bot_id=88803)
    lodger = await member(only_rent, 95004, "5.00")
    kept = await selling.buy_rental(only_rent, lodger, 24, "187", "zz", D("1.04"))
    letter = await selling.buy_email(only_rent, lodger, "openai.com", "OpenAI", "gmx.com", D("0.04"))
    elsewhere = FakeNumberHub(key="nh_live_someone_elses_0123456789")
    real_b, real_p = _bld.NumberHub, _prov.NumberHub
    _bld.NumberHub = _prov.NumberHub = lambda key: (elsewhere if key == elsewhere.key else api).client(key)
    try:
        found = await _bld._an_open_order(only_rent.id)
        check("the shop's running order is named with what it is", found is not None and found[1] in ("rent", "email"), str(found))
        for what, order in (("a rented number", kept), ("a mailbox", letter)):
            open_order = (order.nh_id, order.kind)
            ok_b = await _bld._check_key(own + 1, api.key, open_order)
            ok_p = await _prov._key_problem(api.key, open_order)
            check(f"a key of the SAME account is accepted while {what} runs (it was refused as another account)",
                  ok_b[1] is None and ok_p[1] is None, f"{ok_b[1]} {ok_p[1]}")
            no_b = await _bld._check_key(own + 1, elsewhere.key, open_order)
            no_p = await _prov._key_problem(elsewhere.key, open_order)
            check(f"...and a key of another account is still refused while {what} runs",
                  "different NumberHub account" in (no_b[1] or "") and no_p[1] == "other_account", f"{no_b[1]} {no_p[1]}")
    finally:
        _bld.NumberHub, _prov.NumberHub = real_b, real_p

    # ── the owner's switch ──
    await dp.feed_update(bot, msg(own, "/admin"))
    check("the owner's panel says it is on, with a switch",
          "📅 Rented numbers: <b>on</b>" in session.last_text()
          and button(session, "Rented numbers: on") == Adm(a="rent", arg="off").pack())
    await dp.feed_update(bot, tap(own, button(session, "Rented numbers: on")))
    check("switched off", (await repo.get_reseller(r.id)).rent_on == 0
          and "📅 Rented numbers: <b>off</b>" in session.last_text() and button(session, "Rented numbers: off") is not None)
    await dp.feed_update(bot, msg(cust, "/start"))
    n = len(api.rent_bodies)
    gone = button(session, "Rent a number") is None
    await dp.feed_update(bot, rent("ot", 130))
    check("off: the button is gone and an old button rents nothing", gone and len(api.rent_bodies) == n)
    api.rental_sms(o3.nh_id, "Still here 4455", "Shop", "4455")
    n_sent = len(sent_to(cust))
    await selling.sync_reseller(await repo.get_reseller(r.id))
    check("...and a number rented before keeps delivering its SMS",
          any("<b>Shop</b>: Still here 4455" in x for x in sent_to(cust)[n_sent:]))
    await dp.feed_update(bot, msg(own, "/admin"))
    await dp.feed_update(bot, tap(own, button(session, "Rented numbers: off")))
    await dp.feed_update(bot, msg(cust, "/start"))
    check("switched on again: the button is back", button(session, "Rent a number") is not None)
    api.rent_empty_periods = {720}
    selling._rent_countries.clear()
    await dp.feed_update(bot, tap(cust, Rn(a="home").pack()))
    check("a period with no country to rent in right now is not offered",
          labels()[:3] == ["1 day", "3 days", "1 week"] and "1 month" not in labels(), str(labels()))
    await dp.feed_update(bot, tap(cust, Rn(a="cty", h=720).pack()))
    check("...and its old button says there is nothing to rent, with a way back",
          "Nothing to rent here" in session.last_text() and button(session, "Back") == Rn(a="home").pack())
    api.rent_empty_periods = {24, 72, 168, 720}
    selling._rent_countries.clear()
    await dp.feed_update(bot, tap(cust, Rn(a="home").pack()))
    check("no period has a country: said so, with the way to the menu",
          "Nothing to rent here" in session.last_text() and labels() == ["🏠 Menu"], str(labels()))
    api.rent_empty_periods = set()
    selling._rent_countries.clear()
    api2 = FakeNumberHub()
    api2.rent_periods = []
    r2 = await make_reseller(api2, owner=own, bot_id=88802)
    check("a wallet that rents no numbers: no button, nothing to rent", await selling.rent_periods(r2) == [])

    # ── every language ──
    keys = ["btn_rent", "rent_pick_period", "rent_p_24", "rent_p_72", "rent_p_168", "rent_p_720", "rent_p_days",
            "rent_pick_country", "rent_pick_service", "rent_any_service", "rent_none", "rent_confirm", "btn_rent_buy",
            "rent_card_title", "rent_active_days", "rent_active_hours", "rent_ended", "rent_canceled", "rent_failed",
            "rent_no_sms", "rent_more_sms", "rent_refund_soon", "rent_refund_open", "btn_rent_cancel", "rent_msg_sms",
            "rent_msg_refunded", "rent_msg_ended", "rent_toast_locked", "rent_toast_closed", "rent_toast_sms",
            "rent_toast_refused", "err_rent_limit", "rent_unavailable"]
    missing = [(lang, k) for lang in LANGS for k in keys if k not in _table(lang)]
    same = [(lang, k) for lang in LANGS if lang != "en" for k in keys if _table(lang).get(k) == _table("en")[k]]
    check("the rental texts exist in all 14 languages, translated", not missing and not same, f"{missing[:3]} {same[:3]}")
    now = dt.datetime.now(dt.timezone.utc)
    many = json.dumps([{"from": "Bank", "text": "Use code 5566 to sign in. " * 6, "code": "5566"}] * 7)
    states = (("buying", 0, {}), ("failed", 2, {}), ("canceled", 2, {}), ("completed", 1, {"messages": many}),
              ("waiting", 0, {"cancel_available_at": now + dt.timedelta(minutes=1), "refund_until": now + dt.timedelta(minutes=15)}),
              ("waiting", 0, {"cancel_available_at": now - dt.timedelta(minutes=1), "refund_until": now + dt.timedelta(minutes=15)}),
              ("waiting", 1, {"messages": many}), ("waiting", 1, {}))
    errs = []
    for lang in LANGS:
        mem = Member(id=1, reseller_id=1, telegram_id=1, language=lang, balance=D("1"), held=D("0"))
        for hours in (24, 720, 36):
            for st, settled, extra in states:
                row = Order(id=1, reseller_id=1, member_id=1, kind="rent", service="full", service_name="Any service",
                            country="187", country_name="USA", country_iso="US", phone="17770001", member_price=D("2.60"),
                            status=st, settled=settled, rent_hours=hours, expires_at=now + dt.timedelta(hours=hours), **extra)
                text, kb = order_card(mem, row)
                extra_texts = (rental_sms(mem, row, {"from": "Bank", "text": "Hello", "code": "77"}),
                               rental_refunded(mem, row), rental_ended(mem, row))
                if ("{" in text or not kb.inline_keyboard or len(text) > 3900 or any("{" in x for x in extra_texts)
                        or (extra.get("messages") and "…" not in text and lang == "en")):
                    errs.append(f"{lang}/{st}/{hours}")
    check("the card and the messages render cleanly in every language, state and period, a long SMS list included",
          not errs, str(errs[:5]))
    selling.RENT_SYNC_SEC = slow
    runtime._bots.pop(r.id, None)


async def test_esim():
    """eSIM data plans in a shop: the last product of the wallet that shops did not sell.
    The owner's wallet pays when the plan is ordered; the customer's price is held and
    charged when the wallet reports the eSIM delivered, released if it gives the order up."""
    print("eSIM data plans")
    from aiogram.methods import SendMessage, SendPhoto
    from app.bots.callbacks import Adm, Es, Nav, Ord
    from app.bots.reseller import build_router, esim_confirm_screen
    from app.models import Member
    from app.numberhub import NumberHubError
    from app.texts import esim_card, order_card
    own = 5161
    api = FakeNumberHub()
    r = await make_reseller(api, owner=own, bot_id=88811)
    session = FakeSession(bot_id=88811)
    bot = fake_bot(session)
    runtime._bots[r.id] = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(r.id))
    cust = 96001
    m = await member(r, cust, "10.00")
    labels = lambda: [b.text for row in (session.last_markup().inline_keyboard if session.last_markup() else [])  # noqa: E731
                      for b in row]
    sent_to = lambda uid: [x.text or "" for x in session.requests if isinstance(x, SendMessage) and x.chat_id == uid]  # noqa: E731
    photos = lambda uid: [x for x in session.requests if isinstance(x, SendPhoto) and x.chat_id == uid]  # noqa: E731
    wallet0 = api.wallet
    push_after = selling.ESIM_PUSH_AFTER_SEC
    selling.ESIM_PUSH_AFTER_SEC = -1

    async def state_of(mem):
        fresh = await repo.get_member(mem.id)
        return fresh.balance, fresh.held

    def buy(region, code, cents, uid=cust):
        return tap(uid, Es(a="buy", r=region, k=code, c=cents).pack())

    # ── the way in ──
    await dp.feed_update(bot, msg(cust, "/start"))
    check("the menu has an eSIM data button", button(session, "eSIM data") == Es(a="home").pack())
    await dp.feed_update(bot, tap(cust, button(session, "eSIM data")))
    check("the destinations in the wallet's order, with flags; a group of countries gets a globe",
          labels()[:4] == ["🇺🇸 United States", "🇮🇩 Indonesia", "🌍 Europe (42 areas)", "🇦🇹 Austria"]
          and "type its name" in session.last_text(), str(labels()))
    await dp.feed_update(bot, msg(cust, "euro"))
    check("a typed name finds the destination, with a way back to all of them",
          labels()[0] == "🌍 Europe (42 areas)" and len(labels()) == 3 and button(session, "Back") == Es(a="home").pack(),
          str(labels()))
    await dp.feed_update(bot, msg(cust, "atlantis"))
    check("a name that matches nothing says so and shows the list again",
          "Nothing found" in session.last_text() and labels()[0] == "🇺🇸 United States")
    await dp.feed_update(bot, tap(cust, Es(a="reg", r="ID").pack()))
    check("a destination with no plan says so, with a way back",
          "No plans for <b>Indonesia</b>" in session.last_text() and button(session, "Back") == Es(a="home").pack())
    await dp.feed_update(bot, tap(cust, Es(a="reg", r="US").pack()))
    check("the plans of a destination, cheapest first: data, days and the shop's price",
          labels()[:3] == ["500 MB · 1 day · $0.65", "1 GB · 7 days · $1.30", "5 GB · 30 days · $5.20"]
          and "United States" in session.last_text(), str(labels()))
    api.esim_plans["AT"] = [dict(api.esim_plans["US"][2], code=f"AT{i}", price=f"{1 + i / 100:.2f}") for i in range(95)]
    await dp.feed_update(bot, tap(cust, Es(a="reg", r="AT").pack()))
    check("a destination with 95 plans lists every one of them, in pages",
          len(await selling.esim_plans(r, "AT")) == 95 and any(x == "1/10" for x in labels()), str(labels()[-4:]))
    await dp.feed_update(bot, tap(cust, Es(a="reg", r="EU-42").pack()))
    check("a plan that works in many countries says in how many",
          labels()[0] == "3 GB · 15 days · 🌍 42 · $3.90", str(labels()))
    await dp.feed_update(bot, tap(cust, Es(a="reg", r="US").pack()))
    await dp.feed_update(bot, tap(cust, button(session, "1 GB")))
    text = session.last_text()
    check("the plan is confirmed before anything is bought: data, days, when they start, paid now, no return",
          "United States 1GB 7Days" in text and "Data: <b>1 GB</b>" in text and "<b>7 days</b>" in text
          and "first connects" in text and "Price: <b>$1.30</b>, paid now" in text
          and "Your balance: <b>$10.00</b>" in text and "cannot be returned" in text and not api.esim_bodies
          and button(session, "Buy · $1.30") == Es(a="buy", r="US", k="USA1G7", c=130).pack(), text[:300])

    # ── delivered with the purchase ──
    await dp.feed_update(bot, tap(cust, button(session, "Buy · $1.30")))
    o = (await repo.member_orders(m.id, 1))[0]
    check("delivered with the purchase: charged at once, nothing stays held",
          await state_of(m) == (D("8.70"), D("0")) and o.kind == "esim" and o.status == "completed"
          and o.settled == Order.CHARGED and o.service == "USA1G7" and o.country == "US" and o.codes_announced == 1,
          f"{await state_of(m)} {o.status} {o.settled}")
    check("one purchase at the wallet for exactly this plan, paid there at once",
          api.esim_bodies == [{"pkg": "USA1G7", "region": "US", "max_price": "1.00"}]
          and api.wallet == wallet0 - D("1.00") and api.held == 0, str(api.esim_bodies))
    text = session.last_text()
    check("the card says it is ready and how to install it by hand",
          "eSIM · United States 1GB 7Days" in text and "Your eSIM is ready" in text
          and "SM-DP+ address: <code>rsp.example.net</code>" in text
          and f"Activation code: <code>CODE{o.nh_id}</code>" in text
          and f"<code>LPA:1$rsp.example.net$CODE{o.nh_id}</code>" in text and "Paid: $1.30" in text, text[:400])
    urls = [b.url for row in session.last_markup().inline_keyboard for b in row if b.url]
    check("one tap installs it on an iPhone or on Android",
          len(urls) == 2 and urls[0].startswith("https://esimsetup.apple.com/")
          and urls[1].startswith("https://esimsetup.android.com/"), str(urls))
    check("its QR is sent as a picture the platform drew itself, saying what to do with it",
          len(photos(cust)) == 1 and "Scan this QR code" in (photos(cust)[0].caption or "")
          and type(photos(cust)[0].photo).__name__ == "BufferedInputFile")
    check("the wallet's own picture and short link never reach the customer",
          "source.example" not in " ".join(session.texts()) and "source.example" not in " ".join(str(u) for u in urls))
    await selling.sync_reseller(r)
    await selling.sync_reseller(r)
    check("the order sync sends it no second time", len(photos(cust)) == 1)
    await dp.feed_update(bot, tap(cust, Ord(a="qr", id=o.id).pack()))
    check("the QR button sends the QR again", len(photos(cust)) == 2)
    await dp.feed_update(bot, tap(cust, Ord(a="cancel", id=o.id).pack()))
    check("an eSIM has nothing to cancel: an old cancel button just opens its card",
          (await repo.get_order(o.id)).status == "completed" and await state_of(m) == (D("8.70"), D("0"))
          and "Your eSIM is ready" in session.last_text() and len(photos(cust)) == 2)
    cli = api.client()
    try:
        await cli.number(o.nh_id)
        as_number = "found"
    except NumberHubError as exc:
        as_number = exc.status
    check("it is read as an eSIM, not as a number (a new API key is checked against it that way)",
          as_number == 404 and (await cli.order_of(o.nh_id, "esim"))["esim"]["id"] == o.nh_id)
    await cli.close()

    # ── ready a little later ──
    await dp.feed_update(bot, tap(cust, Es(a="ok", r="EU-42", k="EU3G15").pack()))
    check("a plan for many countries names them, and says when its days start",
          "Works in: France, Germany, Italy, Spain +38" in session.last_text()
          and "as soon as you buy it" in session.last_text(), session.last_text()[:300])
    api.esim_slow = True
    await dp.feed_update(bot, buy("EU-42", "EU3G15", 390))
    o2 = (await repo.member_orders(m.id, 1))[0]
    check("not ready with the purchase: the price is held, the card says it is being prepared, no QR yet",
          await state_of(m) == (D("8.70"), D("3.90")) and o2.status == "waiting" and o2.settled == Order.UNSETTLED
          and "being prepared" in session.last_text() and "$3.90 reserved" in session.last_text()
          and len(photos(cust)) == 2, f"{await state_of(m)} {o2.status}")
    n_sent = len(sent_to(cust))
    await selling.sync_reseller(r)
    check("while it is being prepared nothing is sent and nothing is charged",
          len(sent_to(cust)) == n_sent and len(photos(cust)) == 2 and await state_of(m) == (D("8.70"), D("3.90")))
    api.esim_ready(o2.nh_id)
    await selling.sync_reseller(r)
    await selling.sync_reseller(r)
    o2 = await repo.get_order(o2.id)
    check("when it is ready: charged once, and its QR and its card go out once",
          o2.status == "completed" and o2.settled == Order.CHARGED and await state_of(m) == (D("4.80"), D("0"))
          and len(photos(cust)) == 3 and sum("Your eSIM is ready" in x for x in sent_to(cust)[n_sent:]) == 1
          and o2.codes_announced == 1, f"{o2.status} {await state_of(m)} {len(photos(cust))}")

    # ── the wallet gives an order up ──
    await dp.feed_update(bot, buy("US", "USA1G7", 130))
    o3 = (await repo.member_orders(m.id, 1))[0]
    api.esim_give_up(o3.nh_id)
    n_sent = len(sent_to(cust))
    await selling.sync_reseller(r)
    await selling.sync_reseller(r)
    o3 = await repo.get_order(o3.id)
    check("an eSIM the wallet could not deliver and refunded: the hold is released, the customer is told once",
          o3.status == "canceled" and o3.settled == Order.RELEASED and await state_of(m) == (D("4.80"), D("0"))
          and sum("could not be delivered. $1.30 is back" in x for x in sent_to(cust)[n_sent:]) == 1
          and len(photos(cust)) == 3, f"{o3.status} {await state_of(m)}")
    check("...and its card says so", "could not be delivered — $1.30 returned" in order_card(m, o3)[0])

    # ── what must not buy ──
    api.esim_slow = False
    poor = await member(r, 96002, "0.50")
    n = len(api.esim_bodies)
    await dp.feed_update(bot, buy("US", "USA1G7", 130, uid=96002))
    check("not enough balance: nothing bought, nothing held", "$1.30" in session.last_text()
          and len(api.esim_bodies) == n and await state_of(poor) == (D("0.50"), D("0")))
    await dp.feed_update(bot, buy("US", "USA1G7", 1))
    check("a button made by hand with a lower price buys nothing: the real price to confirm",
          len(api.esim_bodies) == n and "price just changed to <b>$1.30</b>" in session.last_text()
          and button(session, "Buy · $1.30") is not None)
    await dp.feed_update(bot, buy("US", "NOPE", 130))
    check("a plan that is no longer sold: said so, next to the plans that are",
          "can't be bought right now" in session.alerts()[-1] and len(api.esim_bodies) == n
          and "Pick a plan" in session.last_text())
    await dp.feed_update(bot, buy("XX", "USA1G7", 130))
    check("a destination that does not exist shows the destinations", "Where do you need data?" in session.last_text()
          and len(api.esim_bodies) == n)
    api.fail_next_buy = "price_exceeded"
    await dp.feed_update(bot, buy("US", "USA1G7", 130))
    check("the wallet says the price is higher: nothing held, and the price it named is the one to confirm",
          "price just changed to <b>$1.95</b>" in session.last_text() and button(session, "Buy · $1.95") is not None
          and await state_of(m) == (D("4.80"), D("0")), session.last_text()[:160])
    selling._esim_learned.clear()
    selling._esim_plans.clear()
    api.esim_buy_down = True
    await dp.feed_update(bot, buy("US", "USA1G7", 130))
    stuck = [x for x in await repo.stale_buying(older_than_sec=-5) if x.member_id == m.id]
    check("the wallet cannot read its plans at the purchase itself: nothing was bought, nothing stays held or open",
          "Busy right now" in session.last_text() and await state_of(m) == (D("4.80"), D("0")) and not stuck,
          f"{session.last_text()[:60]} {await state_of(m)} {len(stuck)}")
    api.esim_buy_down = False
    n = len(api.esim_bodies)
    await asyncio.gather(dp.feed_update(bot, buy("US", "USA1G7", 130)), dp.feed_update(bot, buy("US", "USA1G7", 130)))
    check("a double tap buys one eSIM", len(api.esim_bodies) == n + 1 and await state_of(m) == (D("3.50"), D("0")),
          f"{len(api.esim_bodies) - n} {await state_of(m)}")
    api.esim_slow = True
    limit = selling.ESIM_MAX_WAITING
    selling.ESIM_MAX_WAITING = 1
    await dp.feed_update(bot, buy("US", "USA1G7", 130))
    waiting = (await repo.member_orders(m.id, 1))[0]
    n = len(api.esim_bodies)
    await dp.feed_update(bot, buy("US", "USA1G7", 130))
    selling.ESIM_MAX_WAITING = limit
    check("a customer with the most eSIMs still being prepared is told so, and nothing is bought",
          "already have 1 eSIMs being prepared" in session.last_text() and len(api.esim_bodies) == n
          and await state_of(m) == (D("3.50"), D("1.30")), session.last_text()[:100])
    api.esim_ready(waiting.nh_id)
    await selling.sync_reseller(r)
    api.esim_slow = False
    shown, asked, pics = len(session.texts()), len(api.calls), len(photos(96002))
    await dp.feed_update(bot, tap(96002, Ord(a="qr", id=o.id).pack()))
    check("another customer can neither open it nor get its QR: told it is closed, the wallet is not asked",
          "closed" in session.alerts()[-1] and len(session.texts()) == shown and len(api.calls) == asked
          and len(photos(96002)) == pics)
    await dp.feed_update(bot, tap(cust, Nav(to="orders").pack()))
    check("My orders lists the eSIMs", sum("🌍" in x and "United States 1GB 7Days" in x for x in labels()) >= 2,
          str(labels()))

    # ── a reply that never came ──
    other = await member(r, 96003, "9.00")
    api.lose_replies = 3
    wallet1, n_orders = api.wallet, len(api.orders)
    try:
        await selling.buy_esim(r, other, "EU-42", "EU3G15", D("3.90"))
        check("three lost replies -> processing", False)
    except SellError as e:
        check("the wallet's answer is lost: 'processing', the customer's money held",
              e.reason == "processing" and await state_of(other) == (D("9.00"), D("3.90")))
    from app import repo as _r
    orig = _r.stale_buying
    _r.stale_buying = lambda older_than_sec=60: orig(older_than_sec=0)
    try:
        await selling.recover_buying()
        await selling.recover_buying()
    finally:
        _r.stale_buying = orig
    await selling.sync_reseller(r)
    rec = (await repo.member_orders(other.id, 1))[0]
    check("recovery asks again with the same request: ONE eSIM, charged once, and the customer gets its QR and card once",
          len(api.orders) == n_orders + 1 and api.wallet == wallet1 - D("3.00") and rec.status == "completed"
          and rec.settled == Order.CHARGED and await state_of(other) == (D("5.10"), D("0"))
          and api.esim_bodies[-1] == {"pkg": "EU3G15", "region": "EU-42", "max_price": "3.00"}
          and len(photos(96003)) == 1 and sum("Your eSIM is ready" in x for x in sent_to(96003)) == 1
          and rec.codes_announced == 1, f"{len(api.orders) - n_orders} {rec.status} {await state_of(other)} {len(photos(96003))}")

    # ── the owner's switch ──
    await dp.feed_update(bot, msg(own, "/admin"))
    check("the owner's panel says it is on, with a switch",
          "🌍 eSIM: <b>on</b>" in session.last_text() and button(session, "eSIM: on") == Adm(a="esim", arg="off").pack())
    await dp.feed_update(bot, tap(own, button(session, "eSIM: on")))
    check("switched off", (await repo.get_reseller(r.id)).esim_on == 0 and "🌍 eSIM: <b>off</b>" in session.last_text()
          and button(session, "eSIM: off") is not None)
    await dp.feed_update(bot, msg(cust, "/start"))
    n = len(api.esim_bodies)
    gone = button(session, "eSIM data") is None
    await dp.feed_update(bot, buy("US", "USA1G7", 130))
    check("off: the button is gone and an old button buys nothing", gone and len(api.esim_bodies) == n)
    await dp.feed_update(bot, tap(cust, Ord(a="view", id=o.id).pack()))
    check("...and an eSIM bought before still opens with how to install it",
          "SM-DP+ address: <code>rsp.example.net</code>" in session.last_text())
    await dp.feed_update(bot, msg(own, "/admin"))
    await dp.feed_update(bot, tap(own, button(session, "eSIM: off")))
    await dp.feed_update(bot, msg(cust, "/start"))
    check("switched on again: the button is back", button(session, "eSIM data") is not None)
    api2 = FakeNumberHub()
    api2.esim_regions_list = []
    r2 = await make_reseller(api2, owner=own, bot_id=88812)
    check("a wallet that sells no eSIM: no button, nothing to buy", await selling.esim_regions(r2) == [])

    # ── every language ──
    keys = ["btn_esim", "esim_pick_region", "esim_none_found", "esim_pick_plan", "esim_no_plans", "esim_confirm",
            "esim_covers", "esim_act_1", "esim_act_2", "esim_act_3", "esim_confirm_pay", "esim_confirm_note",
            "esim_preparing", "esim_ready", "esim_install", "esim_card_closed", "esim_refunded", "esim_failed",
            "esim_held", "btn_esim_ios", "btn_esim_android", "btn_esim_qr", "btn_esim_show", "esim_qr_caption",
            "esim_msg_refunded", "err_esim_limit", "esim_unavailable"]
    missing = [(lang, k) for lang in LANGS for k in keys if k not in _table(lang)]
    same = [(lang, k) for lang in LANGS if lang != "en" for k in keys if _table(lang).get(k) == _table("en")[k]]
    check("the eSIM texts exist in all 14 languages, translated", not missing and not same, f"{missing[:3]} {same[:3]}")
    live = api.esim_public(api.orders[o.nh_id])
    place = {"code": "EU-42", "name": "Europe (42 areas)", "iso": None, "emoji": "🌍"}
    errs = []
    for lang in LANGS:
        mem = Member(id=1, reseller_id=1, telegram_id=1, language=lang, balance=D("1"), held=D("0"))
        for st, settled in (("buying", 0), ("waiting", 0), ("completed", 1), ("canceled", 2), ("failed", 2)):
            row = Order(id=1, reseller_id=1, member_id=1, kind="esim", service="USA1G7",
                        service_name="United States 1GB 7Days", country="US", country_name="United States",
                        country_iso="US", member_price=D("1.30"), status=st, settled=settled)
            for card in (esim_card(mem, row, live), esim_card(mem, row, None), order_card(mem, row)):
                if "{" in card[0] or not card[1].inline_keyboard or len(card[0]) > 3900:
                    errs.append(f"{lang}/{st}")
        for act in (1, 2, 3, None):
            plan = {"code": "EU3G15", "name": "Europe 3GB 15Days", "data": "3 GB", "days": 15, "nh_price": D("3"),
                    "member_price": D("3.90"), "countries": 42, "country_names": ["France", "Germany"],
                    "active_type": act, "region_name": "Europe"}
            text, kb = esim_confirm_screen(mem, lang, place, plan)
            if "{" in text or not kb.inline_keyboard:
                errs.append(f"{lang}/confirm/{act}")
    check("the card and the confirm screen render cleanly in every language and state, with and without the wallet's answer",
          not errs, str(errs[:5]))
    selling.ESIM_PUSH_AFTER_SEC = push_after
    runtime._bots.pop(r.id, None)


async def test_review_defects_2026_11_25():
    """Six defects from the full review of the shop bot (2026-11-24), and a wording bug
    found beside them."""
    print("review defects: late taps, list search, prompt age, price pages, builder notes, payment wording")
    import time
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import DeleteMessage, GetMe, SendMessage
    from aiogram.types import ErrorEvent
    from app import fx, payments
    from app.bots import admin, builder, errors
    from app.bots.callbacks import Adm, Cty, Es, Nav, Pay, Svc
    from app.bots.reseller import build_router
    from app.config import settings
    from app.models import PriceRule
    from fakes import FakeFx, FakeGateways
    own, op = 5171, 424242
    api = FakeNumberHub()
    r = await make_reseller(api, owner=own, bot_id=88821)
    session = FakeSession(bot_id=88821)
    bot = fake_bot(session)
    runtime._bots[r.id] = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(r.id))
    cust = 97001
    await member(r, cust, "5.00")
    labels = lambda: [b.text for row in (session.last_markup().inline_keyboard if session.last_markup() else [])  # noqa: E731
                      for b in row]
    sent_to = lambda uid: [x.text or "" for x in session.requests if isinstance(x, SendMessage) and x.chat_id == uid]  # noqa: E731
    shop = lambda: repo.get_reseller(r.id)     # noqa: E731
    bsession = FakeSession(bot_id=804, username="builder_bot")
    bbot = fake_bot(bsession)
    bdp = Dispatcher(storage=MemoryStorage())
    builder.router._parent_router = None     # other tests attached it to their own dispatcher
    bdp.include_router(builder.router)
    keep = (settings.provision_secret, settings.admin_ids, runtime.swap_key, runtime.start, runtime.restart,
            runtime.make_bot, builder.NumberHub)

    async def fed(d, b, update):
        """What feeding this update raises (None = nothing)."""
        try:
            await d.feed_update(b, update)
        except Exception as exc:  # noqa: BLE001
            return exc
        return None

    # ── a tap answered too late ──
    await dp.feed_update(bot, msg(cust, "/start"))
    shown = len(session.texts())
    session.late_answers = 1
    raised = await fed(dp, bot, tap(cust, Nav(to="orders").pack()))
    check("a tap answered too late: the screen was drawn, and nothing is raised to be logged as an error",
          raised is None and session.late_answers == 0 and len(session.texts()) == shown + 1, repr(raised))
    bsession.late_answers = 1
    raised = await fed(bdp, bbot, tap(own, "bd:create:0:"))
    check("the same in the builder", raised is None and bsession.late_answers == 0, repr(raised))
    session.bad_sends = 1
    raised = await fed(dp, bot, msg(cust, "/start"))
    check("any other refusal by Telegram is still raised, so it is logged with its traceback as before",
          isinstance(raised, TelegramBadRequest) and "chat not found" in str(raised), repr(raised))
    ev = lambda exc: ErrorEvent(update=msg(cust, "x"), exception=exc)     # noqa: E731
    bad = lambda text: TelegramBadRequest(method=GetMe(), message=text)   # noqa: E731
    check("harmless: a late answer and an edit that changes nothing; everything else is not",
          errors.harmless(ev(bad("Bad Request: query is too old and response timeout expired or query ID is invalid")))
          and errors.harmless(ev(bad("Bad Request: message is not modified: specified new message content is the same")))
          and not errors.harmless(ev(bad("Bad Request: message to edit not found")))
          and not errors.harmless(ev(RuntimeError("query is too old"))))

    # ── a typed text and the country list it belongs to ──
    await dp.feed_update(bot, tap(cust, Svc(code="wa", page=0).pack()))
    await dp.feed_update(bot, msg(cust, "us"))
    check("on an app's country list a two-letter code finds a country of that app",
          "<b>WhatsApp</b> — countries for “us”" in session.last_text(), session.last_text()[:80])
    await dp.feed_update(bot, tap(cust, Cty(code="wa", cc="187").pack()))
    await dp.feed_update(bot, msg(cust, "indo"))
    check("...and still on the price screen of one of its countries",
          "countries for “indo”" in session.last_text(), session.last_text()[:80])
    await dp.feed_update(bot, tap(cust, Nav(to="menu").pack()))
    await dp.feed_update(bot, msg(cust, "us"))
    check("after the customer left the list the same text is a search for an app (it gave that app's countries)",
          "countries for" not in session.last_text()
          and ("Results for “us”" in session.last_text() or "Nothing found for “us”" in session.last_text()),
          session.last_text()[:80])
    await dp.feed_update(bot, tap(cust, Svc(code="wa", page=0).pack()))
    await dp.feed_update(bot, tap(cust, Cty(code="wa", cc="187").pack()))
    bought, buy_button = len(api.orders), button(session, "Buy")
    api.fail_next_buy = "sold_out"
    await dp.feed_update(bot, tap(cust, buy_button))
    await dp.feed_update(bot, msg(cust, "indo"))
    check("a purchase that failed keeps the customer on that app's list",
          len(api.orders) == bought and "countries for “indo”" in session.last_text(), session.last_text()[:80])
    await dp.feed_update(bot, tap(cust, buy_button))
    await dp.feed_update(bot, msg(cust, "us"))
    check("a purchase that worked leaves it", len(api.orders) == bought + 1 and "countries for" not in session.last_text(),
          session.last_text()[:80])

    # ── an owner's prompt has a time limit ──
    ctx = dp.fsm.get_context(bot, own, own)
    hour_ago = lambda: ctx.update_data(form_at=time.monotonic() - 3600)   # noqa: E731
    await dp.feed_update(bot, tap(own, Adm(a="welcome").pack()))
    await dp.feed_update(bot, msg(own, "Hello, welcome to my shop"))
    check("a prompt answered in time is saved", (await shop()).welcome_text == "Hello, welcome to my shop")
    await dp.feed_update(bot, tap(own, Adm(a="welcome").pack()))
    await hour_ago()
    await dp.feed_update(bot, msg(own, "whatsapp"))
    check("an hour later the next text is NOT saved (it became the welcome text), and the owner is told",
          (await shop()).welcome_text == "Hello, welcome to my shop" and "Not saved" in session.last_text()
          and button(session, "Admin panel") == Adm(a="home").pack() and await ctx.get_state() is None,
          session.last_text()[:80])
    await dp.feed_update(bot, msg(own, "whatsapp"))
    check("...typed once more it is a search", "Results for “whatsapp”" in session.last_text())
    before = (await shop()).markup_pct
    await dp.feed_update(bot, tap(own, Adm(a="markup", arg="sms").pack()))
    await hour_ago()
    await dp.feed_update(bot, msg(own, "15"))
    check("a number typed into an old commission prompt does not change the commission",
          (await shop()).markup_pct == before == D("30") and "Not saved" in session.last_text())
    await dp.feed_update(bot, tap(own, Adm(a="pay_on", arg=payments.OXAPAY).pack()))
    await hour_ago()
    deleted = sum(isinstance(x, DeleteMessage) for x in session.requests)
    await dp.feed_update(bot, msg(own, "OXA-A-KEY-PASTED-TOO-LATE"))
    check("a gateway key pasted into an old prompt is not stored, and its message is deleted",
          not payments.has_key(await shop(), payments.OXAPAY) and "Not saved" in session.last_text()
          and sum(isinstance(x, DeleteMessage) for x in session.requests) == deleted + 1)
    await dp.feed_update(bot, tap(own, Adm(a="pr_add").pack()))
    await hour_ago()
    await dp.feed_update(bot, tap(own, Adm(a="pr_app", arg="wa").pack()))       # a step inside the prompt
    await dp.feed_update(bot, tap(own, Adm(a="pr_cty", arg="wa|*").pack()))
    await dp.feed_update(bot, msg(own, "0.45"))
    check("each step of a prompt restarts its clock",
          any(x.service == "wa" and x.country == "" and x.value == D("0.45") for x in await repo.price_rules(r.id))
          and "Not saved" not in session.last_text(), session.last_text()[:80])
    cctx = dp.fsm.get_context(bot, cust, cust)
    await dp.feed_update(bot, tap(cust, Es(a="home").pack()))
    await cctx.update_data(form_at=time.monotonic() - 3600)
    await dp.feed_update(bot, msg(cust, "euro"))
    check("a customer's own form has no time limit: its prompt is still the last thing on their screen",
          labels()[0] == "🌍 Europe (42 areas)", str(labels()[:2]))

    # ── the price screen in pages ──
    for i in range(95):
        await repo.set_price_rule(r.id, "wa", str(2000 + i), PriceRule.FIXED, D("0.50"), "WhatsApp",
                                  f"Democratic Republic of Country {i:02d}")
    rules = await repo.price_rules(r.id)
    total = len(rules)
    dels = lambda: [b for row in session.last_markup().inline_keyboard for b in row if b.text.startswith("🗑")]  # noqa: E731
    check("(96 custom prices on one screen are more text than Telegram takes)",
          total == 96 and sum(len(admin.rule_text(x)) + 6 for x in rules) > 4096)
    await dp.feed_update(bot, tap(own, Adm(a="prices").pack()))
    text = session.last_text()
    check("with 96 custom prices the screen shows the first 20, in a message Telegram takes",
          "<b>1–20 of 96</b>" in text and len(text) < 4096 and len(dels()) == 20 and "1/5" in labels()
          and "▶️" in labels() and "◀️" not in labels(), f"{len(text)} {labels()[-6:]}")
    await dp.feed_update(bot, tap(own, Adm(a="prices", arg="4").pack()))
    check("the last page has the rest, and the way back",
          "<b>81–96 of 96</b>" in session.last_text() and len(dels()) == 16 and "5/5" in labels()
          and "◀️" in labels() and "▶️" not in labels(), str(labels()[-6:]))
    await dp.feed_update(bot, tap(own, dels()[0].callback_data))
    check("removing a price keeps the owner on that page",
          "Custom price removed" in session.last_text() and "<b>81–95 of 95</b>" in session.last_text()
          and await repo.count_price_rules(r.id) == 95, session.last_text()[:60])
    await dp.feed_update(bot, tap(own, Adm(a="prices", arg="99").pack()))
    check("a page that no longer exists shows the last one", "<b>81–95 of 95</b>" in session.last_text())
    first = (await repo.price_rules(r.id))[0]
    await dp.feed_update(bot, tap(own, Adm(a="pr_del", arg=str(first.id)).pack()))
    check("a remove button from before the pages still removes its price",
          await repo.count_price_rules(r.id) == 94 and "<b>1–20 of 94</b>" in session.last_text())
    for x in (await repo.price_rules(r.id))[5:]:
        await repo.delete_price_rule(r.id, x.id)
    await dp.feed_update(bot, tap(own, Adm(a="prices").pack()))
    check("a few custom prices: one page, no page buttons, as before",
          len(dels()) == 5 and " of 5" not in session.last_text() and "1/1" not in labels() and "▶️" not in labels())
    for x in await repo.price_rules(r.id):
        await repo.delete_price_rule(r.id, x.id)
    selling.forget_prices(r.id)

    # ── the builder: what a new key changes, and what /enable may lift ──
    swapped, started, restarted = [], [], []

    async def fake_swap(rid, key):
        swapped.append(rid)

    async def fake_start(shop_row):
        started.append(shop_row.id)

    async def fake_restart(rid):
        restarted.append(rid)
    try:
        runtime.swap_key, runtime.start, runtime.restart = fake_swap, fake_start, fake_restart
        runtime.make_bot = lambda token: fake_bot(FakeSession(bot_id=88821, username="my_shop_bot"))
        builder.NumberHub = lambda key: api.client(key)
        builder._key_fails.clear()               # keys other tests had refused on purpose
        builder._all_fails.clear()
        settings.provision_secret, settings.admin_ids = "s" * 40, str(op)
        await repo.update_reseller(r.id, key_by_owner=0)
        await payments.set_builtin(r.id, True)
        check("(a shop the wallet service set up takes built-in payments)", payments.builtin_on(await shop()))
        await bdp.feed_update(bbot, tap(own, f"bd:newkey:{r.id}:"))
        await bdp.feed_update(bbot, msg(own, api.key))
        text = bsession.last_text()
        check("a new key sent by the owner: the answer says built-in payments are now off, and how to get them back",
              "New key saved" in text and "Built-in crypto payments are now off" in text and "@TheNumberHubBot" in text
              and swapped == [r.id] and not payments.builtin_on(await shop()), text[:160])
        await bdp.feed_update(bbot, tap(own, f"bd:newkey:{r.id}:"))
        await bdp.feed_update(bbot, msg(own, api.key))
        check("...said when it happens, not again for a shop already on its owner's key",
              "New key saved" in bsession.last_text() and "Built-in" not in bsession.last_text())
        await repo.update_reseller(r.id, key_by_owner=0)
        await bdp.feed_update(bbot, tap(own, "bd:create:0:"))
        await bdp.feed_update(bbot, msg(own, "123456789:" + "C" * 35))
        await bdp.feed_update(bbot, msg(own, api.key))
        check("a reconnect with a key the owner sends says it too",
              "is reconnected" in bsession.last_text() and "Built-in crypto payments are now off" in bsession.last_text()
              and restarted == [r.id], bsession.last_text()[:160])
        settings.provision_secret = ""
        await repo.update_reseller(r.id, key_by_owner=0)
        await bdp.feed_update(bbot, tap(own, f"bd:newkey:{r.id}:"))
        await bdp.feed_update(bbot, msg(own, api.key))
        check("on a platform its owner runs for themselves nothing is switched off, and nothing is said",
              "New key saved" in bsession.last_text() and "Built-in" not in bsession.last_text()
              and payments.builtin_on(await shop()))

        async def status():
            row = await shop()
            return row.status, row.suspended_from
        started.clear()
        await repo.update_reseller(r.id, status=Reseller.DISABLED)
        await bdp.feed_update(bbot, msg(op, f"/enable {r.id}"))
        check("/enable leaves a shop alone that its owner paused (it set it selling)",
              await status() == (Reseller.DISABLED, None) and not started
              and "not disabled by an operator (paused by its owner)" in bsession.last_text(), bsession.last_text())
        await bdp.feed_update(bbot, msg(op, f"/disable {r.id}"))
        await bdp.feed_update(bbot, msg(op, f"/disable {r.id}"))
        check("/disable, twice: disabled, and what the shop was before is kept",
              await status() == (Reseller.SUSPENDED, Reseller.DISABLED) and "already disabled" in bsession.last_text())
        await bdp.feed_update(bbot, msg(op, f"/enable {r.id}"))
        check("/enable gives a shop back as it was: one its owner paused stays paused",
              await status() == (Reseller.DISABLED, None) and "paused by its owner" in bsession.last_text(),
              bsession.last_text())
        await repo.update_reseller(r.id, status=Reseller.KEY_INVALID)
        await bdp.feed_update(bbot, msg(op, f"/disable {r.id}"))
        await bdp.feed_update(bbot, msg(op, f"/enable {r.id}"))
        check("...and one waiting for a new API key still waits for it",
              await status() == (Reseller.KEY_INVALID, None) and "waiting for a new API key" in bsession.last_text())
        await repo.update_reseller(r.id, status=Reseller.ACTIVE)
        await bdp.feed_update(bbot, msg(op, f"/disable {r.id}"))
        await bdp.feed_update(bbot, msg(own, f"/enable {r.id}"))
        check("a shop owner is no operator: their /enable changes nothing",
              await status() == (Reseller.SUSPENDED, Reseller.ACTIVE))
        await bdp.feed_update(bbot, msg(op, f"/enable {r.id}"))
        check("a selling shop that was disabled sells again",
              await status() == (Reseller.ACTIVE, None) and bsession.last_text().endswith("enabled.")
              and started[-1:] == [r.id], bsession.last_text())
    finally:
        (settings.provision_secret, settings.admin_ids, runtime.swap_key, runtime.start, runtime.restart,
         runtime.make_bot, builder.NumberHub) = keep
        await repo.update_reseller(r.id, status=Reseller.ACTIVE, suspended_from=None)

    # ── payment texts for every way a shop is paid and every product ──
    four = ("err_no_credit_auto", "pay_not_yet", "pay_expired", "pay_too_many")
    crypto = ("crypto", "cripto", "kripto", "крипт", "العملات الرقمية", "ক্রিপ্টো", "رمزارز", "क्रिप्टो", "کرپٹو",
              "tiền mã hóa", "加密")
    link = ("link", "enlace", "lien", "tautan", "ссылк", "bağlantı", "liên kết", "链接", "رابط", "লিংক", "لینک",
            "लिंक", "لنک")
    said = [(lang, k) for lang in LANGS for k in four if any(w in _table(lang)[k].lower() for w in crypto + link)]
    check("no customer text says 'crypto' or 'payment link' for a shop that may be paid by QRIS, in 14 languages",
          not said, str(said[:4]))
    apart = [lang for lang in LANGS
             if _table(lang)["err_no_credit"].split("\n\n")[0] != _table(lang)["err_no_credit_auto"].split("\n\n")[0]
             or "pay_test" not in _table(lang) or (lang != "en" and _table(lang)["pay_test"] == _table("en")["pay_test"])]
    check("both 'not enough balance' texts open with the same sentence, and the test-payment text exists, in 14 languages",
          not apart and "this number" not in _table("en")["err_no_credit"]
          and "this number" not in _table("en")["err_no_credit_auto"], str(apart))
    gw = FakeGateways()
    gw.next = 13000                   # payment ids of its own (7000, 9000 and 11000 are other tests')
    payments.TRANSPORT = gw.transport()
    fakefx = FakeFx()
    fx.TRANSPORT, fx._rate = fakefx.transport(), None
    await fx.refresh()
    await payments.set_builtin(r.id, False)
    await payments.connect(r.id, payments.KASERA, gw.ks_test_key)
    await repo.update_reseller(r.id, kasera_auto=1)      # as the panel does on connecting: today's rupiah rate
    qris_shop = await shop()
    check("(the shop is paid by QRIS only)", payments.connected(qris_shop) == [payments.KASERA])
    selling.forget_prices(r.id)
    broke = 97003
    await member(r, broke, "0")
    await dp.feed_update(bot, tap(broke, Svc(code="wa", page=0).pack()))
    await dp.feed_update(bot, tap(broke, Cty(code="wa", cc="187").pack()))
    await dp.feed_update(bot, tap(broke, button(session, "Buy")))
    text = session.last_text()
    check("a shop paid by QRIS only, a number the balance does not cover: told to top up, with no word of crypto",
          "Not enough balance: the price is" in text and "Add balance below to top up." in text
          and "crypto" not in text.lower(), text[:200])
    await dp.feed_update(bot, tap(broke, Es(a="buy", r="US", k="USA1G7", c=130).pack()))
    text = session.last_text()
    check("an eSIM the balance does not cover is not called a number",
          "the price is <b>$1.30</b>" in text and "number" not in text, text[:200])
    payer = 97002
    pm = await member(r, payer, "0")
    await repo.set_member_language(pm.id, "id")
    p = await payments.open_link(qris_shop, pm.id, payments.KASERA, D("7"))
    gw.ks[p.invoice_id]["status"] = "succeeded"
    await dp.feed_update(bot, tap(payer, Pay(a="chk", id=p.id).pack(), lang="id"))
    check("a test payment the customer checks: told it was a test, in their language (it said 'Not received yet')",
          "mode uji" in session.alerts()[-1] and (await repo.get_member(pm.id)).balance == 0, session.alerts()[-1])
    check("...and the notice the shop sends them is in their language too (it was English only)",
          any("mode uji" in x for x in sent_to(payer)) and not any("Test payment received" in x for x in sent_to(payer)),
          str(sent_to(payer))[:200])
    check("...while the owner is told in the panel's language that the test worked",
          any("Test payment worked" in x for x in sent_to(own)))
    runtime._bots.pop(r.id, None)


async def test_commission_per_product():
    """The platform's owner, 2026-11-25: "commission must be per product". A shop had one
    commission for everything it sells; live shops ran from 0% to 300%."""
    print("a commission for each product")
    from app.bots.callbacks import Adm, Cty, Es, Rn
    from app.bots.reseller import build_router
    from app.models import PriceRule
    own = 5181
    api = FakeNumberHub()
    r = await make_reseller(api, owner=own, bot_id=88831)                # 30%
    other = await make_reseller(api, owner=own + 1, bot_id=88832)
    session = FakeSession(bot_id=88831)
    bot = fake_bot(session)
    runtime._bots[r.id] = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(r.id))
    cust = 98001
    m = await member(r, cust, "20.00")
    labels = lambda: [b.text for row in (session.last_markup().inline_keyboard if session.last_markup() else [])  # noqa: E731
                      for b in row]
    shop = lambda: repo.get_reseller(r.id)     # noqa: E731

    async def prices(row=None):
        """What this shop asks for one item of each product now."""
        row = row or await shop()
        return {
            "sms": next(x for x in await selling.countries(row, "wa") if str(x["country"]) == "187")["member_price"],
            "email": (await selling.email_domains(row, "openai.com"))[0]["member_price"],
            "proxy": next(x for x in await selling.proxy_packages(row) if x["gb"] == 10)["member_price"],
            "rent": next(x for x in await selling.rent_services(row, 24, "187") if x["code"] == "full")["member_price"],
            "esim": next(x for x in await selling.esim_plans(row, "US") if x["code"] == "USA1G7")["member_price"],
        }

    async def set_to(kind, text):
        await dp.feed_update(bot, tap(own, Adm(a="markup", arg=kind).pack()))
        await dp.feed_update(bot, msg(own, text))

    # NumberHub's prices in this test: a number $0.30, a mailbox $0.03, 10 GB $4.90, a day's rental $2.00, an eSIM $1.00.
    start = await prices()
    check("one commission (30%) prices every product, as before",
          start == {"sms": D("0.39"), "email": D("0.04"), "proxy": D("6.37"), "rent": D("2.60"), "esim": D("1.30")},
          str(start))

    # ── the owner's screen ──
    await dp.feed_update(bot, msg(own, "/admin"))
    await dp.feed_update(bot, tap(own, button(session, "Commission")))
    text = session.last_text()
    check("💲 Commission lists the five products with what each is now",
          "📱 Numbers: <b>30%</b>" in text and "🌍 eSIM: <b>30%</b> <i>(same as numbers)</i>" in text
          and text.count("(same as numbers)") == 4
          and labels() == ["📱 Numbers · 30%", "📧 Email codes · 30%", "🌐 Proxy data · 30%", "📅 Rented numbers · 30%",
                           "🌍 eSIM · 30%", "⬅️ Admin panel"], str(labels()))
    await dp.feed_update(bot, msg(own, "40"))
    check("the list itself takes no text: a number typed there changes nothing",
          (await shop()).markup_pct == D("30") and await prices() == start)
    await dp.feed_update(bot, tap(own, Adm(a="markup", arg="esim").pack()))
    check("a product's button asks for its percent, and says how to go back to the numbers' commission",
          "Commission for 🌍 eSIM" in session.last_text() and "Now: <b>30%</b> (same as numbers)" in session.last_text()
          and "Send <code>-</code>" in session.last_text())

    # ── each product by itself ──
    await dp.feed_update(bot, msg(own, "10"))
    now = await prices()
    check("eSIM at 10%: its price follows at once, every other product keeps its price",
          now == dict(start, esim=D("1.10")) and (await shop()).markup_esim == D("10")
          and "Commission for 🌍 eSIM set to <b>10%</b>" in session.last_text()
          and "🌍 eSIM: <b>10%</b>" in session.last_text() and session.last_text().count("(same as numbers)") == 3,
          f"{now} {session.last_text()[:90]}")
    await set_to("rent", "5")
    await set_to("proxy", "0")
    await set_to("email", "100%")
    now = await prices()
    check("rented numbers 5%, proxy data 0% (at NumberHub's price), email codes 100%: each priced by its own",
          now == {"sms": D("0.39"), "email": D("0.06"), "proxy": D("4.90"), "rent": D("2.10"), "esim": D("1.10")},
          str(now))
    check("...and an email price shown before the change is not kept (it stayed until its list ran out)",
          now["email"] != start["email"])
    check("another shop is not touched", await prices(await repo.get_reseller(other.id)) == start)

    # ── what the customer pays and what the order records ──
    await dp.feed_update(bot, tap(cust, Es(a="ok", r="US", k="USA1G7").pack()))
    check("the customer's confirm screen shows the eSIM at the eSIM commission", "Price: <b>$1.10</b>" in session.last_text())
    await dp.feed_update(bot, tap(cust, Es(a="buy", r="US", k="USA1G7", c=110).pack()))
    o = (await repo.member_orders(m.id, 1))[0]
    check("an eSIM bought: the customer pays $1.10, NumberHub is paid $1.00, the order records 10%",
          o.kind == "esim" and o.member_price == D("1.10") and o.nh_price == D("1.00") and o.markup_pct_at_buy == D("10")
          and api.esim_bodies[-1]["max_price"] == "1.00" and (await repo.get_member(m.id)).balance == D("18.90"),
          f"{o.member_price} {o.markup_pct_at_buy} {(await repo.get_member(m.id)).balance}")
    await dp.feed_update(bot, tap(cust, Rn(a="buy", h=24, cc="187", s="full", c=210).pack()))
    o = (await repo.member_orders(m.id, 1))[0]
    check("a number rented: $2.10 held, the order records 5%",
          o.kind == "rent" and o.member_price == D("2.10") and o.markup_pct_at_buy == D("5")
          and (await repo.get_member(m.id)).held == D("2.10"), f"{o.kind} {o.member_price} {o.markup_pct_at_buy}")
    await dp.feed_update(bot, tap(cust, Rn(a="buy", h=24, cc="187", s="full", c=1).pack()))
    check("a button made by hand with a lower price still buys nothing",
          "price just changed to <b>$2.10</b>" in session.last_text() and len(api.rent_bodies) == 1)
    await dp.feed_update(bot, tap(cust, Cty(code="wa", cc="187").pack()))
    await dp.feed_update(bot, tap(cust, button(session, "Buy")))
    o = (await repo.member_orders(m.id, 1))[0]
    check("a number is sold at the numbers' commission, as before",
          o.kind == "sms" and o.member_price == D("0.39") and o.markup_pct_at_buy == D("30"))

    # ── the numbers' commission, and going back to it ──
    await set_to("sms", "50")
    now = await prices()
    check("the numbers' commission changes numbers only: every product with its own keeps it",
          now == {"sms": D("0.45"), "email": D("0.06"), "proxy": D("4.90"), "rent": D("2.10"), "esim": D("1.10")}
          and "Commission set to <b>50%</b>" in session.last_text(), str(now))
    await set_to("esim", "-")
    now = await prices()
    check("'-' puts a product back on the numbers' commission, and it follows it from then on",
          now["esim"] == D("1.50") and (await shop()).markup_esim is None
          and "uses the commission for numbers again (<b>50%</b>)" in session.last_text()
          and "🌍 eSIM: <b>50%</b> <i>(same as numbers)</i>" in session.last_text(), f"{now} {session.last_text()[:90]}")
    await set_to("sms", "-")
    check("numbers have nothing to go back to: '-' is refused and nothing changes",
          "Send a number from 0 to" in session.last_text() and (await shop()).markup_pct == D("50"))
    for bad in ("abc", "-5", "99999", "1e3"):
        await set_to("proxy", bad)
    check("what is no percent is refused for a product too, and its commission stays",
          "Send a number from 0 to" in session.last_text() and "Or <code>-</code>" in session.last_text()
          and (await shop()).markup_proxy == D("0"), session.last_text()[:90])
    await dp.feed_update(bot, tap(cust, Adm(a="markup", arg="esim").pack()))
    await dp.feed_update(bot, msg(cust, "1"))
    check("a customer cannot set a commission", (await shop()).markup_esim is None and (await shop()).markup_pct == D("50"))

    # ── the profit cap ──
    await repo.update_reseller(r.id, max_profit=D("0.10"), markup_rent=D("20"))
    selling.forget_prices(r.id)
    now = await prices()
    check("a profit cap of $0.10 holds numbers and a product that follows them; a product with its own commission is not held",
          now["sms"] == D("0.40") and now["esim"] == D("1.10") and now["rent"] == D("2.40") and now["proxy"] == D("4.90"),
          str(now))
    await dp.feed_update(bot, tap(own, Adm(a="markup").pack()))
    check("the commission screen says what the cap holds",
          "Your profit cap (<b>$0.10</b>) holds numbers and every product that uses the commission for numbers"
          in session.last_text())
    await dp.feed_update(bot, msg(own, "/admin"))
    check("the panel names the products that have their own commission",
          "Own commission: 📧 Email codes <b>100%</b> · 🌐 Proxy data <b>0%</b> · 📅 Rented numbers <b>20%</b>"
          in session.last_text() and "Your commission: <b>50%</b>" in session.last_text(), session.last_text()[:400])
    odp = Dispatcher(storage=MemoryStorage())
    odp.include_router(build_router(other.id))
    osession = FakeSession(bot_id=88832)
    await odp.feed_update(fake_bot(osession), msg(own + 1, "/admin"))
    check("a shop with one commission for everything shows no such line", "Own commission" not in osession.last_text()
          and "Your commission: <b>30%</b>" in osession.last_text())
    for i in range(21):
        await repo.set_price_rule(r.id, "wa", str(3000 + i), PriceRule.FIXED, D("0.50"), "WhatsApp", f"Country {i:02d}")
    await dp.feed_update(bot, tap(own, Adm(a="prices", arg="1").pack()))
    check("a price page with one price is headed '21 of 21' (it read '21–21 of 21')",
          "<b>21 of 21</b>" in session.last_text() and "21–21" not in session.last_text(), session.last_text()[-200:])
    runtime._bots.pop(r.id, None)


async def test_first_payment_path():
    """Live on 2026-11-25: 705 customers had pressed Start in a shop, 28 had ever opened a
    payment. The way to the first payment is counted and shortened here; a QRIS QR says
    how to pay it from the phone it is on; an owner hears of a low wallet before a sale fails."""
    print("the way to the first payment")
    import datetime as _dt
    from aiogram.methods import SendMessage, SendPhoto
    from sqlalchemy import func, select
    from app import fx, payments, reports
    from app.bots import reseller as shop_bot
    from app.bots.callbacks import Adm, Cty, Es, Nav, Pay, Rn, Svc
    from app.bots.reseller import build_router
    from app.db import session_factory
    from app.models import Step
    from fakes import FakeFx, FakeGateways
    own = 5191
    gw = FakeGateways()
    gw.next = 15000                   # payment ids of its own
    payments.TRANSPORT = gw.transport()
    fakefx = FakeFx()
    fx.TRANSPORT, fx._rate = fakefx.transport(), None
    await fx.refresh()
    api = FakeNumberHub()
    r = await make_reseller(api, owner=own, bot_id=88841)
    plain = await make_reseller(api, owner=own + 1, bot_id=88842)        # a shop that takes no automatic payment
    await payments.set_builtin(r.id, False)
    await payments.set_builtin(plain.id, False)
    await payments.connect(r.id, payments.OXAPAY, gw.ox_key)
    session = FakeSession(bot_id=88841)
    bot = fake_bot(session)
    runtime._bots[r.id] = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(r.id))
    shop = lambda: repo.get_reseller(r.id)     # noqa: E731
    labels = lambda: [b.text for row in (session.last_markup().inline_keyboard if session.last_markup() else [])  # noqa: E731
                      for b in row]
    sent_to = lambda uid: [x.text or "" for x in session.requests if isinstance(x, SendMessage) and x.chat_id == uid]  # noqa: E731
    photos = lambda uid: [x for x in session.requests if isinstance(x, SendPhoto) and x.chat_id == uid]  # noqa: E731
    cust, looker = 99001, 99002
    m = await member(r, cust, "0.20")

    async def rows():
        async with session_factory() as s:
            return int(await s.scalar(select(func.count()).select_from(Step).where(Step.reseller_id == r.id)) or 0)

    # ── one tap to the money that is missing ──
    await dp.feed_update(bot, msg(cust, "/start"))
    await dp.feed_update(bot, tap(cust, Nav(to="buy").pack()))
    await dp.feed_update(bot, tap(cust, Svc(code="wa", page=0).pack()))
    await dp.feed_update(bot, tap(cust, Cty(code="wa", cc="187").pack()))
    check("a number at $0.39 with $0.20 on the balance: the button adds the smallest payment the shop takes",
          button(session, "Add $1.00") == Pay(a="amt", c=100).pack() and "💳 Add balance" not in labels(), str(labels()))
    await dp.feed_update(bot, tap(cust, button(session, "Buy")))
    check("after a purchase the balance did not cover: the same one tap, and the way back to the number",
          "Not enough balance" in session.last_text() and button(session, "Add $1.00") == Pay(a="amt", c=100).pack()
          and button(session, "Back") == Cty(code="wa", cc="187").pack(), str(labels()))
    await dp.feed_update(bot, tap(cust, Rn(a="ok", h=24, cc="16", s="full").pack()))
    check("a rental at $3.90: the button adds exactly what is missing ($3.70)",
          button(session, "Add $3.70") == Pay(a="amt", c=370).pack(), str(labels()))
    await dp.feed_update(bot, tap(cust, Es(a="ok", r="US", k="USA5G30").pack()))
    check("an eSIM at $5.20: $5.00 is missing", button(session, "Add $5.00") == Pay(a="amt", c=500).pack(), str(labels()))
    await dp.feed_update(bot, tap(cust, Rn(a="ok", h=24, cc="16", s="full").pack()))
    await dp.feed_update(bot, tap(cust, button(session, "Add $3.70")))
    opened = await repo.open_payments(m.id)
    check("the tap opens a payment for that amount at once (one way to pay: no further question)",
          len(opened) == 1 and opened[0].amount == D("3.70") and opened[0].provider == payments.OXAPAY,
          str([(p.amount, p.provider) for p in opened]))
    low, high = payments.limits(await shop())
    from aiogram.utils.keyboard import InlineKeyboardBuilder as _KB
    kb = _KB()
    check("more than one payment can be, or nothing missing: no such button (the caller keeps its own)",
          not shop_bot._gap_button(kb, await shop(), await repo.get_member(m.id), "en", high + 5)
          and not shop_bot._gap_button(kb, await shop(), await repo.get_member(m.id), "en", D("0.10"))
          and not list(kb.buttons))
    psession = FakeSession(bot_id=88842)
    pbot = fake_bot(psession)
    pdp = Dispatcher(storage=MemoryStorage())
    pdp.include_router(build_router(plain.id))
    await member(plain, cust, "0.20")
    await pdp.feed_update(pbot, tap(cust, Cty(code="wa", cc="187").pack()))
    check("a shop that takes no automatic payment keeps the plain Add balance button",
          button(psession, "Add balance") == Nav(to="balance").pack() and button(psession, "Add $") is None)

    # ── the steps are counted ──
    f = await repo.funnel(await shop(), 30)
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    check("one customer's way so far: opened, looked, saw a price, tapped Buy, was short, opened the top-up, started a payment",
          {k: f[k] for k in ("open", "browse", "price", "buy", "short", "topup", "pay", "paid", "bought")}
          == {"open": 1, "browse": 1, "price": 1, "buy": 1, "short": 1, "topup": 1, "pay": 1, "paid": 0, "bought": 0}
          and f["since"] == today, str(f))
    before = await rows()
    for _ in range(3):
        await dp.feed_update(bot, tap(cust, Svc(code="wa", page=0).pack()))
        await dp.feed_update(bot, tap(cust, Cty(code="wa", cc="187").pack()))
    check("the same steps again on the same day add no row (one row per customer, day and step)", await rows() == before == 6)
    await member(r, looker, "0")
    await dp.feed_update(bot, msg(looker, "/start"))
    await dp.feed_update(bot, msg(own, "/start"))
    await dp.feed_update(bot, tap(own, Svc(code="wa", page=0).pack()))
    f = await repo.funnel(await shop(), 30)
    check("a customer who only opened the bot counts once at the top; the shop's owner is not counted at all",
          f["open"] == 2 and f["browse"] == 1 and f["price"] == 1, str(f))
    gw.ox[opened[0].invoice_id]["status"] = "Paid"
    await payments.poll_all()
    await dp.feed_update(bot, tap(cust, Cty(code="wa", cc="187").pack()))
    await dp.feed_update(bot, tap(cust, button(session, "Buy")))
    number = (await repo.member_orders(m.id, 1))[0]
    api.set_status(number.nh_id, "received", "112233")
    await selling.sync_reseller(await shop())
    f = await repo.funnel(await shop(), 30)
    check("the payment arrives and a number gets its code: paid and bought are read from the money itself",
          f["paid"] == 1 and f["bought"] == 1 and (await repo.get_member(m.id)).balance == D("3.51"),
          f"{f} {(await repo.get_member(m.id)).balance}")
    everywhere = await repo.funnel(None, 30)
    check("all shops together count at least this shop", all(everywhere[k] >= f[k] for k in ("open", "price", "pay", "paid")))
    sp = lambda data: shop_bot._step_of(tap(cust, data).callback_query)     # noqa: E731
    check("every product's buttons are told apart by what they are for",
          [sp(x) for x in (Es(a="buy", r="US", k="A", c=1).pack(), Rn(a="ok", h=24, cc="1", s="full").pack(),
                           "px:pkg:5:0", "e:dom:1:0:abc", "e:home:0:0:", Es(a="home").pack(), Nav(to="orders").pack(),
                           Pay(a="amt", c=100).pack(), Nav(to="deposit").pack(), "o:view:5")]
          == ["buy", "price", "price", "price", "browse", "browse", None, "topup", "topup", None]
          and shop_bot._step_of(msg(cust, "whatsapp").message) == "browse" and shop_bot._step_of(msg(cust, "/start").message) is None)
    await dp.feed_update(bot, tap(own, Adm(a="stats").pack()))
    text = session.last_text()
    check("the owner's statistics show how far their customers got, one line a step",
          "From a visit to a purchase" in text and f"counted since {today}" in text
          and "Opened the bot: <b>2</b>" in text and "Saw a price: <b>1</b>" in text and "Were short of balance: <b>1</b>" in text
          and "Started a payment: <b>1</b>" in text and "Paid: <b>1</b>" in text and "Got what they bought: <b>1</b>" in text,
          text[-420:])
    untouched = await make_reseller(api, owner=own + 2, bot_id=88843)
    check("a shop where no step was counted yet shows no such block",
          "From a visit" not in await reports.stats_text(untouched, 30))

    # ── QRIS: how to pay from the phone the QR is on ──
    await payments.connect(r.id, payments.KASERA, gw.ks_key)
    await repo.update_reseller(r.id, kasera_auto=1)
    await repo.set_member_language((await member(r, 99003, "0")).id, "id")
    await dp.feed_update(bot, tap(99003, Pay(a="via", g="kq", c=100).pack(), lang="id"))
    caption = (photos(99003)[-1].caption or "") if photos(99003) else ""
    check("the QR's caption says how to pay from the same phone, in the customer's language",
          "Bayar dari HP ini?" in caption and "galeri" in caption, caption[-160:])
    await dp.feed_update(bot, tap(looker, Cty(code="wa", cc="187").pack()))
    check("a shop that takes QRIS from $0.50: the button for a $0.39 number adds $0.50",
          button(session, "Add $0.50") == Pay(a="amt", c=50).pack(), str(labels()))
    long = [lang for lang in LANGS if len(_table(lang)["pay_qris"].format(idr="Rp 1.000.000", amount="$60.00", minutes=60)
                                          + "\n\n" + _table(lang)["pay_qris_phone"]) > 1024 or "pay_qris_phone" not in _table(lang)
            or "btn_topup_gap" not in _table(lang)]
    check("both new texts exist in all 14 languages, and the caption fits Telegram's 1,024 characters in each", not long, str(long))

    # ── the owner hears of a low wallet before a sale fails ──
    keep_pause = reports.WALLET_PAUSE_SEC
    reports.WALLET_PAUSE_SEC = 0
    try:
        async def watch(now=None):
            """What the owner of this shop is told by one look at the wallets."""
            n = len(sent_to(own))
            await reports.wallet_watch(now)
            return sent_to(own)[n:]
        check("a wallet with money in it: nothing is sent", await watch() == [])
        api.wallet = D("0.30")
        told = await watch()
        check("the wallet falls to $0.30 while customers use the shop: the owner is told, with what customers hold "
              "and that their own gateway's money does not reach the wallet",
              len(told) == 1 and "Your NumberHub wallet is low: $0.30" in told[0]
              and "Your customers hold <b>$3.51</b>" in told[0] and "through your own gateway" in told[0]
              and "numberhub.io" in told[0], str(told)[:300])
        check("not twice on the same day, also after a restart (it is kept in the database)",
              await watch() == [] and await repo.get_setting(f"wallet_low:{r.id}") == today)
        tomorrow = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=1)
        check("still low the next day: told once more", len(await watch(tomorrow)) == 1)
        api.wallet = D("2.00")
        day3 = tomorrow + _dt.timedelta(days=1)
        check("$2.00 against sales that cost $0.30 a day: enough, nothing is sent", await watch(day3) == [])
        api.wallet = D("10.00")
        big = await member(r, 99004, "9.00")
        await selling.buy_esim(await shop(), big, "US", "USA5G30", D("5.20"))
        api.wallet = D("2.00")
        told = await watch(day3)
        check("the same $2.00 after a day in which sales cost $4.30: less than a day of sales, the owner is told",
              len(told) == 1 and "In the last 24 hours your sales cost <b>$4.30</b>" in told[0], str(told)[:200])
        quiet = await make_reseller(FakeNumberHub(wallet="0.05"), owner=own + 3, bot_id=88844)
        qsession = FakeSession(bot_id=88844)
        runtime._bots[quiet.id] = fake_bot(qsession)
        await reports.wallet_watch(day3 + _dt.timedelta(days=1))
        check("an empty wallet in a shop nobody uses: its owner is left alone", not qsession.texts())
        runtime._bots.pop(quiet.id, None)
        await payments.set_builtin(r.id, True)
        text = reports.wallet_low_text(await shop(), D("0.30"), D("0"), D("0"))
        check("a shop with built-in payments is told that those land in the wallet by themselves",
              payments.builtin_on(await shop()) and "built-in crypto lands in this wallet by itself" in text
              and "sales cost" not in text and "hold" not in text, text[-200:])
    finally:
        reports.WALLET_PAUSE_SEC = keep_pause
    runtime._bots.pop(r.id, None)


async def test_every_mailbox():
    """The platform's owner, 2026-11-25: "still email domain static, we can do any domain".
    The wallet offered 47 kinds of mailbox for any site; a shop showed the first 10, which
    for most sites are the 10 cheapest, so gmail.com was never among them."""
    print("email codes: every mailbox of a site")
    from app.bots.callbacks import Em
    from app.bots.reseller import EMAIL_DOMAINS_PAGE, _tok, build_router
    own = 5201
    api = FakeNumberHub()
    # As the wallet lists them for a site with no history: cheapest first, gmail.com last.
    cheap = ["mail.com", "gmx.com", "email.com"] + [f"gmx.{x}" for x in ("us", "fr", "es", "it", "pt", "se", "ie", "ca")]
    mid = [f"yandex.{x}" for x in ("com", "kz", "by", "lt", "lv", "ee", "fi", "fr", "kg", "md", "tj", "tm", "uz", "az")]
    listed = ([{"domain": d, "price": "0.01", "in_stock": True, "delivery": None} for d in cheap]
              + [{"domain": d, "price": "0.02", "in_stock": True, "delivery": None} for d in mid]
              + [{"domain": "libero.it", "price": "0.02", "in_stock": True, "delivery": "low"},
                 {"domain": "caramail.fr", "price": "0.02", "in_stock": True, "delivery": "high"},
                 {"domain": "icloud.com", "price": "0.03", "in_stock": True, "delivery": None},
                 {"domain": "gmail.com", "price": "0.04", "in_stock": True, "delivery": None}])
    api.email_domains["shop-site.io"] = listed
    r = await make_reseller(api, owner=own, bot_id=88851)
    session = FakeSession(bot_id=88851)
    bot = fake_bot(session)
    runtime._bots[r.id] = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(r.id))
    cust = 99101
    m = await member(r, cust, "5.00")
    labels = lambda: [b.text for row in (session.last_markup().inline_keyboard if session.last_markup() else [])  # noqa: E731
                      for b in row]
    names = lambda: [x.split(" ")[1] for x in labels() if "·" in x and "$" in x]     # noqa: E731

    await dp.feed_update(bot, msg(cust, "/start"))
    await dp.feed_update(bot, tap(cust, button(session, "Email codes")))
    text = session.last_text()
    check("the first screen says any website and asks for its address before it shows a list",
          "Email codes for any website" in text and "Type the website's address" in text and "Any website works" in text
          and text.index("Type the website's address") < text.index("Or tap one that is often asked for"), text[:200])
    await dp.feed_update(bot, msg(cust, "shop-site.io"))
    first = names()
    check("29 mailboxes for the site: the first page leads with the one that delivers best here, then the names "
          "everybody knows, gmail.com and icloud.com among them (they were never shown)",
          len(listed) == 29 and first[:6] == ["caramail.fr", "gmail.com", "icloud.com", "gmx.com", "mail.com", "email.com"]
          and len(first) == EMAIL_DOMAINS_PAGE == 10 and "1/3" in labels() and "➡️" in labels(), str(first))
    check("the screen says how many there are and that a name can be typed",
          "29 kinds of mailbox" in session.last_text() and "<code>gmail</code>" in session.last_text())
    seen = list(first)
    await dp.feed_update(bot, tap(cust, button(session, "➡️")))
    seen += names()
    check("page two has ten more", len(names()) == 10 and "2/3" in labels() and "⬅️" in labels())
    await dp.feed_update(bot, tap(cust, button(session, "➡️")))
    seen += names()
    check("the last page has the rest, with the one that delivers badly for this site at the very end",
          len(names()) == 9 and names()[-1] == "libero.it" and "3/3" in labels() and "➡️" not in labels(), str(names()))
    check("every mailbox the wallet offers can be reached, each once",
          sorted(seen) == sorted(x["domain"] for x in listed), f"{len(seen)} {len(set(seen))}")
    await dp.feed_update(bot, tap(cust, button(session, "yandex.tm")))
    back = button(session, "Back")
    check("a mailbox from page three: its price to confirm",
          "yandex.tm" in session.last_text() and button(session, "Get the address") is not None
          and back == Em(a="doms", i=2, t=_tok("shop-site.io")).pack(),
          session.last_text()[:80])
    await dp.feed_update(bot, tap(cust, back))
    check("Back returns to the page it was on, not to the first", "3/3" in labels() and "yandex.tm" in names())

    # ── a typed name ──
    await dp.feed_update(bot, msg(cust, "gmail"))
    check("'gmail' typed on the list finds gmail.com", names() == ["gmail.com"] and "shop-site.io" in session.last_text(),
          str(labels()))
    await dp.feed_update(bot, msg(cust, "Yandex"))
    check("'Yandex' finds all 14 yandex mailboxes, and Back shows every mailbox again",
          len(names()) == 14 and all(x.startswith("yandex.") for x in names())
          and button(session, "Back") == Em(a="doms", t=_tok("shop-site.io")).pack(), str(names()))
    await dp.feed_update(bot, tap(cust, button(session, "yandex.kz")))
    await dp.feed_update(bot, tap(cust, button(session, "Get the address")))
    o = (await repo.member_orders(m.id, 1))[0]
    check("the mailbox bought is the one tapped in the found list",
          o.kind == "email" and o.country == "yandex.kz" and o.service == "shop-site.io" and o.member_price == D("0.03"),
          f"{o.kind} {o.country} {o.member_price}")
    await dp.feed_update(bot, tap(cust, button(session, "Email codes") or Em(a="home").pack()))
    await dp.feed_update(bot, msg(cust, "shop-site.io"))
    await dp.feed_update(bot, msg(cust, "discord.com"))
    check("a website's address typed on a mailbox list is still a website (no mailbox has that name)",
          "discord.com" in session.last_text() and "shop-site.io" not in session.last_text(), session.last_text()[:80])
    await dp.feed_update(bot, tap(cust, Em(a="home").pack()))
    await dp.feed_update(bot, msg(cust, "gmail"))
    check("on the list of sites the same word is a search for a site, not for a mailbox",
          names() == [] and "kinds of mailbox" not in session.last_text(), str(labels()))
    api.email_domains["small.io"] = listed[:4]
    await dp.feed_update(bot, msg(cust, "small.io"))
    check("a site with a few mailboxes: one page, no page buttons, no hint to type",
          len(names()) == 4 and "1/1" not in labels() and "kinds of mailbox" not in session.last_text())
    missing = [lang for lang in LANGS if "email_type_domain" not in _table(lang)
               or (lang != "en" and _table(lang)["email_type_domain"] == _table("en")["email_type_domain"])]
    check("the new line exists in all 14 languages, translated", not missing, str(missing))
    runtime._bots.pop(r.id, None)


async def main():
    await init_db()
    test_prices()
    test_i18n()
    for fn in (test_buy_and_code, test_no_code_refund, test_failures, test_lost_reply, test_cancel,
               test_races_and_limits, test_bot_flow, test_builder, test_provision, test_custom_prices, test_picker_pages, test_rank_order, test_dead_token, test_rejected_key, test_deposits, test_auto_payments, test_builtin_payments, test_owner_stats, test_kasera_qris, test_paid_left, test_email_codes, test_paused_shop, test_review_money,
               test_review_sync, test_review_bots, test_review_runtime, test_small_topups_and_rupiah,
               test_proxy_data, test_rentals, test_esim, test_review_2026_11_24, test_number_cycling,
               test_review_defects_2026_11_25, test_commission_per_product, test_first_payment_path,
               test_every_mailbox,
               test_back_to_the_purchase,
               test_cards_render):
        try:
            await fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(f"{fn.__name__} crashed", False, repr(exc))
    print(f"\nRESULT: {P} passed, {F} failed")
    return F


if __name__ == "__main__":
    failed = asyncio.run(main())
    sys.stdout.flush()
    os._exit(1 if failed else 0)
