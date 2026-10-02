"""Entry point: the builder bot, every reseller bot, and the order sync loops."""
from __future__ import annotations

import asyncio
import logging
import signal

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app import repo, runtime, selling
from app.bots import builder
from app.config import settings
from app.db import init_db

log = logging.getLogger("reseller")


async def sync_loop() -> None:
    while True:
        for rid in runtime.running():
            try:
                reseller = await repo.get_reseller(rid)
                if reseller is not None:
                    await selling.sync_reseller(reseller)
            except Exception:  # noqa: BLE001 — one reseller's trouble never stops the rest
                log.exception("sync failed for reseller %s", rid)
        await asyncio.sleep(settings.sync_interval_sec)


async def sweep_loop() -> None:
    while True:
        try:
            await selling.settle_sweep()
            await selling.recover_buying()
        except Exception:  # noqa: BLE001
            log.exception("sweep failed")
        await asyncio.sleep(30)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not settings.builder_bot_token or not settings.secret_key:
        raise SystemExit("BUILDER_BOT_TOKEN and SECRET_KEY must be set (see .env.example)")
    await init_db()
    bot = runtime.make_bot(settings.builder_bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(builder.router)
    await runtime.start_all()
    loops = [asyncio.create_task(sync_loop()), asyncio.create_task(sweep_loop())]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False, close_bot_session=False))
    try:
        await asyncio.wait([polling, asyncio.create_task(stop.wait())], return_when=asyncio.FIRST_COMPLETED)
    finally:
        try:
            await dp.stop_polling()
        except Exception:  # noqa: BLE001
            pass
        # Let purchases in flight finish before closing anything (a member's hold
        # must not be stranded between their tap and the NumberHub order).
        inflight = runtime.inflight() + [t for t in getattr(dp, "_handle_update_tasks", ()) if not t.done()]
        if inflight:
            await asyncio.wait(inflight, timeout=60)
        for t in loops:
            t.cancel()
        await runtime.stop_all()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
