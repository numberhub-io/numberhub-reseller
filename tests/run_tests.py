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
    await dp.feed_update(bot, tap(OWNER, Adm(a="markup").pack()))
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
        settings.provision_secret = "short"
        check("a short secret keeps the listener off", await provision.start_server() is None)
    finally:
        await client.close()
        rt.make_bot, rt.start, rt.restart, provision.NumberHub, settings.provision_secret = orig


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
    async with engine.begin() as conn:
        await conn.execute(_text("ALTER TABLE orders DROP COLUMN ceiling_at_buy"))
        await conn.execute(_text("ALTER TABLE resellers DROP COLUMN max_profit"))
    await _init()
    async with engine.begin() as conn:
        cols = {row[1] for row in (await conn.execute(_text("PRAGMA table_info(orders)"))).all()}
        rcols = {row[1] for row in (await conn.execute(_text("PRAGMA table_info(resellers)"))).all()}
    check("start-up adds the new columns to a database from before",
          "ceiling_at_buy" in cols and "max_profit" in rcols)


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
    await dp.feed_update(bot, tap(OWNER, button(session, "Deposits")))
    check("admin: 💳 Deposits explains it and says it is off", "Off." in session.last_text())
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
          and button(session, "Deposits (1)") is not None)
    await dp.feed_update(bot, tap(OWNER, button(session, "Deposits")))
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
    await dp.feed_update(bot, tap(OWNER, Adm(a="markup").pack()))
    check("commission prompt shows plain numbers (no '3E+1')", "E+" not in session.last_text())
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


async def main():
    await init_db()
    test_prices()
    test_i18n()
    for fn in (test_buy_and_code, test_no_code_refund, test_failures, test_lost_reply, test_cancel,
               test_races_and_limits, test_bot_flow, test_builder, test_provision, test_custom_prices, test_picker_pages, test_deposits, test_paused_shop, test_review_money,
               test_review_sync, test_review_bots, test_review_runtime, test_cards_render):
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
