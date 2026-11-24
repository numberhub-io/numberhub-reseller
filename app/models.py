"""Tables. Money is Numeric and handled as Decimal everywhere.

Two kinds of money, never mixed:
  * the reseller's NumberHub wallet — real money, held and charged by NumberHub
    for every number their members buy (we only read it through the API);
  * a member's balance here — credit the reseller sells to their own customers:
    paid by crypto through the reseller's own OxaPay / @CryptoBot and credited
    automatically (app/payments.py), or paid their own way and added in the admin
    panel. A purchase holds the member's price; when the order ends it is charged
    (a code arrived) or released.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Reseller(Base):
    """One selling bot: the reseller's Telegram bot + their NumberHub API key."""
    __tablename__ = "resellers"
    ACTIVE = "active"
    DISABLED = "disabled"      # paused by the reseller (they can resume it)
    SUSPENDED = "suspended"    # disabled by a platform operator (only /enable lifts it)
    KEY_INVALID = "key_invalid"  # NumberHub rejected the API key (revoked/rotated)
    TOKEN_INVALID = "token_invalid"  # Telegram rejects the bot token, or another service took the bot

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)          # reseller's Telegram id
    bot_token_enc: Mapped[str] = mapped_column(Text)
    bot_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    bot_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bot_title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    api_key_enc: Mapped[str] = mapped_column(Text)
    api_key_hint: Mapped[str | None] = mapped_column(String(16), nullable=True)
    markup_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal("30"))
    # Most the shop earns on one number, in dollars (None = no cap): with a percent
    # commission an expensive number would otherwise cost the member a lot more.
    max_profit: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    # Deposits: what customers see when they add balance (how to pay the owner).
    # None = deposits off (customers send their ID to the support contact instead).
    deposit_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    deposit_min: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    # Automatic top-ups (app/payments.py) with the owner's OWN gateway account,
    # encrypted like the bot token. None = not connected.
    oxapay_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    cryptobot_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # KaseraPay (Indonesia, QRIS), the owner's own account: its API key, and how many
    # rupiah one dollar costs in this shop (balances are in dollars; the owner sets it).
    kasera_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    kasera_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    # Built-in USDT payments (app/payments.py, provider "numberhub"): the customer
    # pays to an address NumberHub opens with the shop's own API key, and the money
    # is added to the shop's NumberHub wallet. 1 = on, 0 = off, None = the
    # platform's default (settings.numberhub_pay_default).
    nh_pay: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 1 = the shop runs on an API key its owner pasted (the builder bot); empty or 0 =
    # the key was handed over by the operator's wallet service and nobody else has
    # seen it. Only such a key can have what customers pay in kept for the shop, so
    # on a platform run for others only such a shop offers built-in payments.
    key_by_owner: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Email codes in this shop: 0 = the owner switched them off, anything else on.
    email_on: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The morning report to the owner (app/reports.py): 0 = off, anything else on.
    # report_day = the UTC day it was last sent for, so it goes out once a day.
    daily_report: Mapped[int | None] = mapped_column(Integer, nullable=True)
    report_day: Mapped[str | None] = mapped_column(String(10), nullable=True)
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
    # The part of `balance` this customer paid in with built-in payments and has not
    # spent: real money, sitting in the shop's NumberHub wallet. Up when such a
    # payment is credited, down when an order is charged (paid-in money goes first).
    # The rest of the balance was added by the owner (by hand, an approved deposit,
    # or the owner's own gateway) and has only the owner's own money behind it.
    # Always between 0 and `balance`. The owner cannot take it away.
    paid_left: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"), server_default="0")
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    # Last bought service/country, for the one-tap "Buy again".
    last_service: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_country: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    @property
    def available(self) -> Decimal:
        return Decimal(self.balance or 0) - Decimal(self.held or 0)

    @property
    def by_hand(self) -> Decimal:
        """The part of the balance that was not paid in with built-in payments."""
        return max(Decimal("0"), Decimal(self.balance or 0) - Decimal(self.paid_left or 0))


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
    # Nothing more can happen. Any other status NumberHub reports (received, or a
    # transient one such as requesting/reactivating) keeps being polled.
    TERMINAL = ("completed", "canceled", "expired", "failed")
    # settled
    UNSETTLED = 0
    CHARGED = 1
    RELEASED = 2

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    reseller_id: Mapped[int] = mapped_column(ForeignKey("resellers.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    nh_id: Mapped[int | None] = mapped_column(nullable=True)
    # "sms" = a phone number for a code by SMS or call; "email" = a mailbox for one
    # mail from one site. An email order keeps the site in `service`, the mail domain
    # in `country` and the mailbox address in `phone`, as NumberHub's own orders do.
    kind: Mapped[str] = mapped_column(String(8), default="sms", server_default="sms")
    # Why a purchase did not happen (NumberHub's own answer: insufficient_funds,
    # sold_out, price_exceeded, ...), for the owner's statistics. None otherwise.
    fail_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    service: Mapped[str] = mapped_column(String(128))
    service_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country: Mapped[str] = mapped_column(String(128))
    country_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country_iso: Mapped[str | None] = mapped_column(String(2), nullable=True)   # for the flag
    phone: Mapped[str | None] = mapped_column(String(255), nullable=True)
    member_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    # The markup in force when bought: the purchase request (and its replay after a
    # lost reply) is rebuilt from it, not from today's setting.
    markup_pct_at_buy: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal("0"))
    # The NumberHub price ceiling sent with the purchase. A custom price breaks the
    # "member price = ceiling + commission" relation, so the recovery replay (which
    # must send the SAME body) reads it from here. None on orders from before.
    ceiling_at_buy: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
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


class PriceRule(Base):
    """The shop's own price for one app (country "") or one app in one country:
    a fixed price, or its own commission percent. The most specific rule wins:
    app + country, then the app, then the shop's commission. A fixed price never
    goes below NumberHub's price for the number (selling.shop_price)."""
    __tablename__ = "price_rules"
    __table_args__ = (UniqueConstraint("reseller_id", "service", "country", name="uq_price_rule"),)
    FIXED = "fixed"
    PCT = "pct"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    reseller_id: Mapped[int] = mapped_column(ForeignKey("resellers.id", ondelete="CASCADE"), index=True)
    service: Mapped[str] = mapped_column(String(16))
    service_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country: Mapped[str] = mapped_column(String(16), default="")
    country_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mode: Mapped[str] = mapped_column(String(8))
    value: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Deposit(Base):
    """A customer's request to add balance: they paid the owner the owner's way
    and ask to be credited. Credited exactly once, when the owner approves
    (repo.decide_deposit: the status change and the credit are one transaction)."""
    __tablename__ = "deposits"
    __table_args__ = (UniqueConstraint("reseller_id", "number", name="uq_deposit_number"),)
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    reseller_id: Mapped[int] = mapped_column(ForeignKey("resellers.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column()                      # #1, #2, ... per shop
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))    # what the customer says they paid
    credited: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    proof_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    proof_photo: Mapped[str | None] = mapped_column(String(256), nullable=True)   # Telegram file_id
    status: Mapped[str] = mapped_column(String(16), default=PENDING, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Payment(Base):
    """An automatic top-up: a payment link from the owner's own OxaPay or
    @CryptoBot, or a built-in USDT address payment into the shop's NumberHub
    wallet. Credited exactly once, by repo.credit_payment (from pending only, in
    the same transaction as the balance change)."""
    __tablename__ = "payments"
    __table_args__ = (UniqueConstraint("provider", "invoice_id", name="uq_payment_invoice"),)
    PENDING = "pending"        # link open, not paid yet
    PAID = "paid"              # credited
    EXPIRED = "expired"        # never paid (or refunded)
    FAILED = "failed"          # the gateway refused to make the link
    MANUAL = "manual"          # the owner accepted a short payment in their dashboard: added by hand

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    reseller_id: Mapped[int] = mapped_column(ForeignKey("resellers.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(16))           # oxapay | cryptobot | numberhub
    invoice_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pay_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A built-in payment has no link: JSON {method, address, pay_amount,
    # pay_currency, network, network_short} that the bot shows instead.
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    credited: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=PENDING, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

