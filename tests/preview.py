"""Print every screen a customer and the owner see (text + buttons), to review
the UX without Telegram:   python tests/preview.py [lang]"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
_tmp = tempfile.TemporaryDirectory()
os.environ["DB_URL"] = "sqlite+aiosqlite:///" + (Path(_tmp.name) / "p.db").as_posix()
from cryptography.fernet import Fernet  # noqa: E402

os.environ["SECRET_KEY"] = Fernet.generate_key().decode()
os.chdir(_tmp.name)

from aiogram import Dispatcher  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402

from app import repo, runtime, selling  # noqa: E402
from app.bots.callbacks import Adm  # noqa: E402
from app.bots.reseller import build_router  # noqa: E402
from app.db import init_db  # noqa: E402
from fakes import FakeNumberHub, FakeSession, fake_bot  # noqa: E402
from run_tests import make_reseller, msg, tap, button  # noqa: E402

LANG = sys.argv[1] if len(sys.argv) > 1 else "en"


def show(title, session):
    m = session.requests[-1]
    for req in reversed(session.requests):
        if type(req).__name__ in ("SendMessage", "EditMessageText"):
            m = req
            break
    print(f"\n━━━━━━━━━━ {title} ━━━━━━━━━━")
    print(m.text)
    if m.reply_markup:
        for row in m.reply_markup.inline_keyboard:
            print("   " + "  ".join(f"[{b.text}]" for b in row))


async def main():
    await init_db()
    api = FakeNumberHub()
    r = await make_reseller(api)
    s = FakeSession()
    bot = fake_bot(s)
    runtime._bots[r.id] = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(r.id))
    U = 7001
    await dp.feed_update(bot, msg(U, "/start", LANG)); show("menu (new customer)", s)
    await dp.feed_update(bot, tap(U, "n:buy", lang=LANG)); show("services", s)
    await dp.feed_update(bot, msg(U, "insta", LANG)); show("search", s)
    await dp.feed_update(bot, tap(U, "s:wa:0", lang=LANG)); show("countries", s)
    await dp.feed_update(bot, tap(U, "c:wa:187", lang=LANG)); show("confirm (no balance)", s)
    await dp.feed_update(bot, tap(U, button(s, "·") or "b:wa:187:39", lang=LANG)); show("buy without balance", s)
    m = await repo.find_member(r.id, str(U))
    await repo.member_adjust(r.id, m.id, __import__("decimal").Decimal("3"))
    await dp.feed_update(bot, tap(U, "c:wa:187", lang=LANG)); show("confirm", s)
    await dp.feed_update(bot, tap(U, "b:wa:187:39", lang=LANG)); show("order card (waiting)", s)
    o = (await repo.member_orders(m.id, 1))[0]
    api.set_status(o.nh_id, "received", "482913")
    await selling.sync_reseller(r)
    print("\n━━━━━━━━━━ code message ━━━━━━━━━━"); print([x for x in s.texts() if "482913" in x][0])
    show("order card (received)", s)
    await dp.feed_update(bot, tap(U, "n:orders", lang=LANG)); show("my orders", s)
    await dp.feed_update(bot, tap(U, "n:balance", lang=LANG)); show("balance", s)
    await dp.feed_update(bot, tap(U, "n:menu", lang=LANG)); show("menu (returning customer)", s)
    await dp.feed_update(bot, msg(5001, "/admin")); show("owner: admin panel", s)
    await dp.feed_update(bot, tap(5001, Adm(a="add").pack())); show("owner: add balance prompt", s)
    await dp.feed_update(bot, tap(5001, Adm(a="members").pack())); show("owner: customers", s)


asyncio.run(main())
sys.stdout.flush()
os._exit(0)
