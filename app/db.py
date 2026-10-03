"""Async engine, session factory and schema creation."""
from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models import Base

_is_sqlite = settings.db_url.startswith("sqlite")
engine = create_async_engine(settings.db_url, echo=False,
                             connect_args={"timeout": 30} if _is_sqlite else {})
session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

if _is_sqlite:
    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        # WAL: the sync loop writes while bots read; busy_timeout waits for the
        # write lock instead of failing a money update.
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


# Columns added after a table first shipped: create_all() makes new tables but
# never alters an existing one, so an older database gets them here.
_ADDED_COLUMNS = (
    ("resellers", "max_profit", "NUMERIC(12, 2)"),
    ("orders", "ceiling_at_buy", "NUMERIC(12, 2)"),
)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if _is_sqlite:
            from sqlalchemy import text
            for table, column, kind in _ADDED_COLUMNS:
                have = {row[1] for row in (await conn.execute(text(f"PRAGMA table_info({table})"))).all()}
                if column not in have:
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {kind}"))
