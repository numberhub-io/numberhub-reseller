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


async def warm_loop() -> None:
    """The popular apps' "from" prices, refreshed in the background."""
    await asyncio.sleep(3)
    while True:
        for rid in runtime.running():
            try:
                reseller = await repo.get_reseller(rid)
                if reseller is not None:
                    await selling.warm_popular(reseller)
            except Exception:  # noqa: BLE001
                log.exception("price warm-up failed for reseller %s", rid)
        await asyncio.sleep(120)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not settings.secret_key:
        raise SystemExit("SECRET_KEY must be set (see .env.example)")
    await init_db()
    # Without BUILDER_BOT_TOKEN only the reseller bots run (a single self-hosted
    # shop, or a test): nobody can create new bots, everything else works.
    bot = runtime.make_bot(settings.builder_bot_token) if settings.builder_bot_token else None
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(builder.router)
    if bot is None:
        log.warning("BUILDER_BOT_TOKEN not set: running reseller bots only")
    await runtime.start_all()
    loops = [asyncio.create_task(sync_loop()), asyncio.create_task(sweep_loop()),
             asyncio.create_task(warm_loop())]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    polling = (asyncio.create_task(dp.start_polling(bot, handle_signals=False, close_bot_session=False))
               if bot is not None else asyncio.create_task(stop.wait()))
    try:
        await asyncio.wait([polling, asyncio.create_task(stop.wait())], return_when=asyncio.FIRST_COMPLETED)
    finally:
        if bot is not None:
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
        if bot is not None:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
