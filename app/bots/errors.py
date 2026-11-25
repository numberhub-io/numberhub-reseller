"""Telegram answers that are no fault of the code are dropped quietly, so that a real
error stands out in the log.

Until 2026-11-25 the shops and the builder had no error handler: a button tapped while
the service restarted was answered too late for Telegram ("query is too old"), and that
was logged as an ERROR with a full traceback at every release."""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ErrorEvent

log = logging.getLogger(__name__)

# A tap answered too late (the screen was drawn, only "stop the spinner" was refused),
# and an edit that changes nothing (the same button tapped twice).
HARMLESS = ("query is too old", "query id is invalid", "message is not modified")


def harmless(event: ErrorEvent) -> bool:
    exc = event.exception
    return isinstance(exc, TelegramBadRequest) and any(s in str(exc).lower() for s in HARMLESS)


async def _drop(event: ErrorEvent) -> bool:
    log.info("update dropped: %s", event.exception)
    return True


def install(router: Router) -> None:
    """Only the harmless answers are handled here. Every other exception finds no
    handler, so it is raised and logged with its traceback exactly as before."""
    router.errors.register(_drop, harmless)
