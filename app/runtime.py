"""Runs every reseller bot in this process: one aiogram Bot + Dispatcher each,
plus that reseller's NumberHub API client for the selling code."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramConflictError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import GetUpdates
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


# Consecutive getUpdates conflicts before the shop gives the bot up: another
# service keeps setting a webhook (or polling) with the same token.
CONFLICTS_BEFORE_GIVING_UP = 5


class _TokenWatch(BaseRequestMiddleware):
    """Sees every getUpdates answer. aiogram retries a failed poll forever, so a
    revoked token (Unauthorized) or a token another service keeps using
    (Conflict: a webhook set elsewhere) polled every 5 s for a whole day:
    2,400 errors from two paused shops on 2026-10-04. Either one ends this
    shop's polling and marks it for reconnecting; its open orders keep syncing."""

    def __init__(self, reseller_id: int):
        self.reseller_id = reseller_id
        self.conflicts = 0
        self.tripped = False

    async def __call__(self, make_request, bot, method):
        if not isinstance(method, GetUpdates):
            return await make_request(bot, method)
        try:
            result = await make_request(bot, method)
        except TelegramUnauthorizedError:
            self._trip("Telegram rejects the token (revoked)")
            raise
        except TelegramConflictError:
            self.conflicts += 1
            if self.conflicts >= CONFLICTS_BEFORE_GIVING_UP:
                self._trip("another service is using the token (webhook or polling elsewhere)")
            raise
        self.conflicts = 0
        return result

    def _trip(self, why: str) -> None:
        if self.tripped:
            return
        self.tripped = True
        log.error("reseller bot %s: %s; polling stopped until the owner sends the token again",
                  self.reseller_id, why)
        asyncio.create_task(_give_up_token(self.reseller_id))


async def _give_up_token(reseller_id: int) -> None:
    reseller = await repo.get_reseller(reseller_id)
    if reseller is not None and reseller.status != Reseller.SUSPENDED:
        await repo.update_reseller(reseller_id, status=Reseller.TOKEN_INVALID)
    await _halt_polling(reseller_id)


def bot_for(reseller_id: int) -> Bot | None:
    return _bots.get(reseller_id)


def running() -> list[int]:
    return [rid for rid, task in _tasks.items() if not task.done()]


def known() -> list[int]:
    """Shops whose orders must be kept in sync: every shop with a NumberHub
    client, whether or not its Telegram polling is up right now."""
    return list(selling._clients)


async def start(reseller: Reseller) -> None:
    """Paused shops run too: their customers are told the shop is paused, and
    orders bought before the pause still get their codes and settle (a stopped
    bot left those holds, and codes NumberHub had already billed, stranded)."""
    if reseller.id in running():
        return
    from app.bots.reseller import build_router
    try:
        token, key = crypto.decrypt(reseller.bot_token_enc), crypto.decrypt(reseller.api_key_enc)
    except Exception:  # noqa: BLE001 — SECRET_KEY changed: the owner must reconnect
        log.error("reseller %s: stored token/key can't be decrypted (SECRET_KEY changed?); "
                  "the owner has to reconnect the bot in the builder", reseller.id)
        return
    selling.set_client(reseller.id, NumberHub(key))
    if reseller.status == Reseller.TOKEN_INVALID:
        # Its orders keep syncing (the client above); polling waits for a new
        # token, so a restart does not fight the service now holding the bot.
        log.info("reseller bot %s (@%s): token needs reconnecting, not polling",
                 reseller.id, reseller.bot_username)
        return
    bot = make_bot(token)
    bot.session.middleware(_TokenWatch(reseller.id))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(reseller.id))
    _bots[reseller.id], _dps[reseller.id] = bot, dp
    _tasks[reseller.id] = asyncio.create_task(_run(reseller.id, bot, dp), name=f"reseller-{reseller.id}")
    log.info("reseller bot %s (@%s) started", reseller.id, reseller.bot_username)


async def _run(reseller_id: int, bot: Bot, dp: Dispatcher) -> None:
    """Poll Telegram for this shop until stopped. A failed start (getMe timing
    out once at boot) is retried with backoff instead of leaving the shop dead;
    only a revoked token ends it."""
    delay = 5
    while True:
        try:
            try:
                await bot.set_my_commands(COMMANDS)
            except Exception:  # noqa: BLE001 — cosmetic
                pass
            # A bot that ran elsewhere may still have a webhook, and Telegram
            # refuses getUpdates while one is set: the shop would never answer.
            await bot.delete_webhook(drop_pending_updates=False)
            await dp.start_polling(bot, handle_signals=False, close_bot_session=False,
                                   allowed_updates=dp.resolve_used_update_types())
            return                       # stop_polling() was called
        except asyncio.CancelledError:
            raise
        except TelegramUnauthorizedError:
            log.error("reseller bot %s: Telegram rejected the token (revoked?); the owner has to "
                      "reconnect it in the builder", reseller_id)
            await _give_up_token(reseller_id)
            return
        except Exception:  # noqa: BLE001
            log.exception("reseller bot %s stopped with an error; retrying in %ss", reseller_id, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)


def _handler_tasks(dp: Dispatcher) -> list[asyncio.Task]:
    return [t for t in getattr(dp, "_handle_update_tasks", ()) if not t.done()]


async def _halt_polling(reseller_id: int) -> None:
    dp = _dps.get(reseller_id)
    if dp is not None:
        try:
            await dp.stop_polling()
        except Exception:  # noqa: BLE001 — never started / already stopping
            pass


async def stop(reseller_id: int, wait: float = 60) -> None:
    """Stop taking updates, let purchases in flight finish, then close."""
    await _halt_polling(reseller_id)
    dp, task, bot = _dps.pop(reseller_id, None), _tasks.pop(reseller_id, None), _bots.pop(reseller_id, None)
    pending = _handler_tasks(dp) if dp is not None else []
    if pending:
        await asyncio.wait(pending, timeout=wait)
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


async def swap_key(reseller_id: int, key: str) -> None:
    """A new API key, without restarting the bot. The old client stays open for
    a while so a purchase in flight on it can finish (closing it under one made
    that purchase fail while NumberHub may have bought the number)."""
    old = selling.client_for(reseller_id)
    selling.set_client(reseller_id, NumberHub(key))
    if reseller_id not in running():
        reseller = await repo.get_reseller(reseller_id)
        if reseller is not None:
            await start(reseller)
    if old is not None:
        async def later():
            await asyncio.sleep(180)
            await old.close()
        asyncio.create_task(later())


async def start_all() -> None:
    for reseller in await repo.list_resellers():
        try:
            await start(reseller)
        except Exception:  # noqa: BLE001 — one bad bot must not stop the others
            log.exception("reseller bot %s failed to start", reseller.id)


async def stop_all(wait: float = 60) -> None:
    """Shutdown: every shop stops taking updates FIRST, then the purchases in
    flight across all shops get up to `wait` seconds, then everything closes."""
    await asyncio.gather(*(_halt_polling(rid) for rid in list(_dps)), return_exceptions=True)
    pending = inflight()
    if pending:
        await asyncio.wait(pending, timeout=wait)
    for rid in list(_tasks):
        await stop(rid, wait=0)
    for rid in list(selling._clients):       # shops whose bot never came up
        cli = selling.drop_client(rid)
        if cli is not None:
            await cli.close()


def inflight() -> list[asyncio.Task]:
    out = []
    for dp in _dps.values():
        out.extend(_handler_tasks(dp))
    return out
