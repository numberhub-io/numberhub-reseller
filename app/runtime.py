"""Runs every active reseller bot in this process: one aiogram Bot + Dispatcher
each, plus that reseller's NumberHub API client for the selling code."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app import crypto, repo, selling
from app.models import Reseller
from app.numberhub import NumberHub

log = logging.getLogger(__name__)
_bots: dict[int, Bot] = {}
_dps: dict[int, Dispatcher] = {}
_tasks: dict[int, asyncio.Task] = {}

COMMANDS = [
    BotCommand(command="start", description="Main menu"),
    BotCommand(command="buy", description="Buy a number"),
    BotCommand(command="orders", description="My orders"),
    BotCommand(command="balance", description="My balance and ID"),
]


def make_bot(token: str) -> Bot:
    return Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def bot_for(reseller_id: int) -> Bot | None:
    return _bots.get(reseller_id)


def running() -> list[int]:
    return [rid for rid, task in _tasks.items() if not task.done()]


async def start(reseller: Reseller) -> None:
    """Paused shops run too: their customers are told the shop is paused, and
    orders bought before the pause still get their codes and settle (a stopped
    bot left those holds, and codes NumberHub had already billed, stranded)."""
    if reseller.id in running():
        return
    from app.bots.reseller import build_router
    token, key = crypto.decrypt(reseller.bot_token_enc), crypto.decrypt(reseller.api_key_enc)
    selling.set_client(reseller.id, NumberHub(key))
    bot = make_bot(token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(reseller.id))
    _bots[reseller.id], _dps[reseller.id] = bot, dp
    _tasks[reseller.id] = asyncio.create_task(_run(reseller.id, bot, dp), name=f"reseller-{reseller.id}")
    log.info("reseller bot %s (@%s) started", reseller.id, reseller.bot_username)


async def _run(reseller_id: int, bot: Bot, dp: Dispatcher) -> None:
    try:
        try:
            await bot.set_my_commands(COMMANDS)
        except Exception:  # noqa: BLE001 — cosmetic
            pass
        await dp.start_polling(bot, handle_signals=False, close_bot_session=True,
                               allowed_updates=dp.resolve_used_update_types())
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("reseller bot %s stopped with an error", reseller_id)


async def stop(reseller_id: int) -> None:
    dp, task, bot = _dps.pop(reseller_id, None), _tasks.pop(reseller_id, None), _bots.pop(reseller_id, None)
    if dp is not None:
        try:
            await dp.stop_polling()
        except Exception:  # noqa: BLE001 — never started / already stopping
            pass
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout=15)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            task.cancel()
    if bot is not None:
        try:
            await bot.session.close()
        except Exception:  # noqa: BLE001
            pass
    cli = selling.drop_client(reseller_id)
    if cli is not None:
        await cli.close()
    log.info("reseller bot %s stopped", reseller_id)


async def restart(reseller_id: int) -> None:
    await stop(reseller_id)
    reseller = await repo.get_reseller(reseller_id)
    if reseller is not None:
        await start(reseller)


async def start_all() -> None:
    for reseller in await repo.list_resellers():
        try:
            await start(reseller)
        except Exception:  # noqa: BLE001 — one bad bot must not stop the others
            log.exception("reseller bot %s failed to start", reseller.id)


async def stop_all() -> None:
    for rid in list(_tasks):
        await stop(rid)


def inflight() -> list[asyncio.Task]:
    out = []
    for dp in _dps.values():
        out.extend(t for t in getattr(dp, "_handle_update_tasks", ()) if not t.done())
    return out
