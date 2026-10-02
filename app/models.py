"""Tables. Money is Numeric and handled as Decimal everywhere.

Two kinds of money, never mixed:
  * the reseller's NumberHub wallet — real money, held and charged by NumberHub
    for every number their members buy (we only read it through the API);
  * a member's balance here — credit the reseller sells to their own customers
    their own way and adds in the admin panel. A purchase holds the member's
    price; when the order ends it is charged (a code arrived) or released.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Reseller(Base):
    """One selling bot: the reseller's Telegram bot + their NumberHub API key."""
    __tablename__ = "resellers"
    ACTIVE = "active"
    DISABLED = "disabled"      # by the reseller or a platform operator
    KEY_INVALID = "key_invalid"  # NumberHub rejected the API key (revoked/rotated)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)          # reseller's Telegram id
    bot_token_enc: Mapped[str] = mapped_column(Text)
    bot_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    bot_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bot_title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    api_key_enc: Mapped[str] = mapped_column(Text)
    api_key_hint: Mapped[str | None] = mapped_column(String(16), nullable=True)
    markup_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal("30"))
    welcome_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    support_contact: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=ACTIVE, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Member(Base):
    """A customer of one reseller's bot."""
    __tablename__ = "members"
    __table_args__ = (UniqueConstraint("reseller_id", "telegram_id", name="uq_member"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    reseller_id: Mapped[int] = mapped_column(ForeignKey("resellers.id", ondelete="CASCADE"), index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    language: Mapped[str] = mapped_column(String(8), default="en")
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    held: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    # Last bought service/country, for the one-tap "Buy again".
    last_service: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_country: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    @property
    def available(self) -> Decimal:
        return Decimal(self.balance or 0) - Decimal(self.held or 0)


class MemberTx(Base):
    """Every change to a member's balance: the reseller's credits and debits and
    the charge of each delivered order."""
    __tablename__ = "member_tx"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    reseller_id: Mapped[int] = mapped_column(ForeignKey("resellers.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))             # credit | debit | charge
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))   # signed
    order_id: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class Order(Base):
    """A number a member bought: the local side of one NumberHub order."""
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("reseller_id", "nh_id", name="uq_nh_order"),)
    BUYING = "buying"          # member credit held, the NumberHub call is in flight
    FAILED = "failed"          # the purchase did not happen; credit released
    # NumberHub's own statuses, mirrored: pending, waiting, received, completed,
    # canceled, expired.
    OPEN = ("buying", "pending", "waiting")
    CHARGED_STATUSES = ("received", "completed")
    # settled
    UNSETTLED = 0
    CHARGED = 1
    RELEASED = 2

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    reseller_id: Mapped[int] = mapped_column(ForeignKey("resellers.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    nh_id: Mapped[int | None] = mapped_column(nullable=True)
    service: Mapped[str] = mapped_column(String(16))
    service_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country: Mapped[str] = mapped_column(String(16))
    country_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country_iso: Mapped[str | None] = mapped_column(String(2), nullable=True)   # for the flag
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    member_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    # The markup in force when bought: the purchase request (and its replay after a
    # lost reply) is rebuilt from it, not from today's setting.
    markup_pct_at_buy: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal("0"))
    nh_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=BUYING, index=True)
    codes: Mapped[str] = mapped_column(Text, default="[]")
    codes_announced: Mapped[int] = mapped_column(default=0)
    settled: Mapped[int] = mapped_column(default=UNSETTLED, index=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_available_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
