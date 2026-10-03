"""Entry point: the builder bot, every reseller bot, and the order sync loops."""
from __future__ import annotations

import asyncio
import logging
import signal

from aiogram import Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage

from app import db, provision, repo, runtime, selling
from app.bots import builder
from app.config import settings

log = logging.getLogger("reseller")
SYNC_PARALLEL = 8          # shops synced at the same time (each has its own API key budget)
WARM_PARALLEL = 4


def _cancelling() -> bool:
    task = asyncio.current_task()
    return bool(task and task.cancelling())


async def _each_shop(fn, label: str, parallel: int) -> None:
    """Run fn(reseller) for every shop, a few at a time, so one slow shop (a
    busy NumberHub key, Telegram flood limits) never delays the others."""
    gate = asyncio.Semaphore(parallel)

    async def one(rid: int):
        async with gate:
            try:
                reseller = await repo.get_reseller(rid)
                if reseller is not None:
                    await fn(reseller)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — one reseller's trouble never stops the rest
                if _cancelling():
                    raise
                log.exception("%s failed for reseller %s", label, rid)

    await asyncio.gather(*(one(rid) for rid in runtime.known()))


async def sync_loop() -> None:
    while True:
        await _each_shop(selling.sync_reseller, "sync", SYNC_PARALLEL)
        await asyncio.sleep(settings.sync_interval_sec)


async def sweep_loop() -> None:
    while True:
        try:
            await selling.settle_sweep()
            await selling.recover_buying()
            await selling.release_orphans()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            if _cancelling():
                raise
            log.exception("sweep failed")
        await asyncio.sleep(30)


async def warm_loop() -> None:
    """The popular apps' "from" prices, refreshed in the background."""
    await asyncio.sleep(3)
    while True:
        await _each_shop(selling.warm_popular, "price warm-up", WARM_PARALLEL)
        selling.prune_caches()
        await asyncio.sleep(120)


async def builder_loop(bot, dp: Dispatcher, stop: asyncio.Event) -> None:
    """Poll the builder bot until shutdown. A failed start is retried with
    backoff: before, one getMe timeout at boot ended polling, which tore down
    every shop and left a process that never exited (so systemd never
    restarted it)."""
    delay = 5
    while not stop.is_set():
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            await dp.start_polling(bot, handle_signals=False, close_bot_session=False)
            return
        except asyncio.CancelledError:
            raise
        except TelegramUnauthorizedError:
            log.error("BUILDER_BOT_TOKEN was rejected by Telegram: the builder is off, shops keep running")
            return
        except Exception:  # noqa: BLE001
            log.exception("builder bot stopped with an error; retrying in %ss", delay)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 300)


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not settings.secret_key:
        raise SystemExit("SECRET_KEY must be set (see .env.example)")
    await db.init_db()
    # Without BUILDER_BOT_TOKEN only the reseller bots run (a single self-hosted
    # shop, or a test): nobody can create new bots, everything else works.
    bot = runtime.make_bot(settings.builder_bot_token) if settings.builder_bot_token else None
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(builder.router)
    if bot is None:
        log.warning("BUILDER_BOT_TOKEN not set: running reseller bots only")
    await runtime.start_all()
    provisioning = await provision.start_server()
    loops = [asyncio.create_task(sync_loop()), asyncio.create_task(sweep_loop()),
             asyncio.create_task(warm_loop())]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    builder_task = asyncio.create_task(builder_loop(bot, dp, stop)) if bot is not None else None
    try:
        await stop.wait()
    finally:
        log.info("shutting down: no new updates, waiting for purchases in flight")
        if provisioning is not None:
            await provisioning.cleanup()      # no new hosted shops while the others stop
        if bot is not None:
            try:
                await dp.stop_polling()
            except Exception:  # noqa: BLE001
                pass
        builder_inflight = [t for t in getattr(dp, "_handle_update_tasks", ()) if not t.done()]
        # Every shop stops taking updates first, then purchases in flight get up
        # to 60 s, and only then are the HTTP clients closed (closing one under
        # a purchase made it fail while NumberHub may have bought the number).
        await asyncio.gather(runtime.stop_all(wait=60),
                             *([asyncio.wait(builder_inflight, timeout=60)] if builder_inflight else []))
        for t in loops + ([builder_task] if builder_task else []):
            t.cancel()
        await asyncio.gather(*loops, *([builder_task] if builder_task else []), return_exceptions=True)
        if bot is not None:
            await bot.session.close()
        # aiosqlite keeps a non-daemon thread per pooled connection: without
        # dispose() the interpreter never exits and systemd can't restart it.
        await db.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
