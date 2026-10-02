"""Test doubles: an in-memory NumberHub API (httpx MockTransport) and a fake
Telegram Bot API session for aiogram, so the real code runs end to end."""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import httpx
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageText, GetMe, SendMessage, SetMyCommands
from aiogram.types import Message, User

UTC = dt.timezone.utc


def iso(v: dt.datetime | None) -> str | None:
    return v.astimezone(UTC).isoformat().replace("+00:00", "Z") if v else None


class FakeNumberHub:
    """Behaves like api.numberhub.io/v1 for one API key."""

    def __init__(self, key: str = "nh_live_testkey_0123456789abcdef", wallet: str = "50.00"):
        self.key = key
        self.wallet = Decimal(wallet)
        self.held = Decimal("0")
        self.orders: dict[int, dict] = {}
        self.next_id = 1000
        self.calls: list[tuple[str, str]] = []
        self.idem: dict[str, tuple[int, dict]] = {}
        self.countries = {
            "wa": [
                {"country": "187", "name": "USA", "flag": "US", "price": "0.30", "price_max": "0.30",
                 "in_stock": True, "rate": 62, "rate_low": False, "rate_dead": False, "collapsed": False},
                {"country": "6", "name": "Indonesia", "flag": "ID", "price": "0.20", "price_max": "0.25",
                 "in_stock": True, "rate": None, "rate_low": False, "rate_dead": False, "collapsed": False},
                {"country": "0", "name": "Russia", "flag": "RU", "price": "0.50", "price_max": "0.50",
                 "in_stock": False, "rate": 5, "rate_low": True, "rate_dead": False, "collapsed": False},
            ],
            "tg": [{"country": "187", "name": "USA", "flag": "US", "price": "1.00", "price_max": "1.10",
                    "in_stock": True, "rate": 40, "rate_low": False, "rate_dead": False, "collapsed": False}],
        }
        self.services = [{"code": "wa", "name": "WhatsApp"}, {"code": "tg", "name": "Telegram"},
                         {"code": "ig", "name": "Instagram"}]
        # knobs
        self.fail_next_buy: str | None = None       # an error code for the next POST /numbers
        self.lose_replies = 0                        # POSTs that act, then "time out"
        self.cancel_lock = 0                         # seconds_remaining for DELETE
        self.cancel_code_first = False
        self.reject_key = False
        # (service, country) -> the price POST /numbers really reserves, when the
        # list under-reports it (live NumberHub did this from 09-23 to 10-02).
        self.true_reserve: dict[tuple[str, str], str] = {}

    # ── state helpers for tests ──
    def set_status(self, nh_id: int, status: str, code: str | None = None) -> None:
        o = self.orders[nh_id]
        o["status"] = status
        if code:
            o["codes"].append(code)
            o["code"] = code
        if status in ("received", "completed") and not o.get("_charged"):
            o["_charged"] = True
            self.held -= Decimal(o["price"])
            self.wallet -= Decimal(o["price"])
        if status in ("canceled", "expired") and not o.get("_charged") and not o.get("_released"):
            o["_released"] = True
            self.held -= Decimal(o["price"])

    def public(self, o: dict) -> dict:
        return {k: v for k, v in o.items() if not k.startswith("_")}

    # ── HTTP ──
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self, key: str | None = None):
        """A real client talking to this fake; `key` = the key the client sends."""
        from app.numberhub import NumberHub
        return NumberHub(key or self.key, base="https://fake.test/v1",
                         http=httpx.AsyncClient(transport=self.transport()))

    def _json(self, status: int, body: dict, headers=None) -> httpx.Response:
        return httpx.Response(status, json=body, headers=headers or {})

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/v1", "", 1)
        self.calls.append((request.method, path))
        if request.headers.get("authorization") != f"Bearer {self.key}" or self.reject_key:
            return self._json(401, {"error": "unauthorized"})
        if request.method == "GET" and path == "/balance":
            return self._json(200, {"balance": f"{self.wallet:.2f}", "available": f"{self.wallet - self.held:.2f}",
                                    "held": f"{self.held:.2f}", "currency": "USD"})
        if request.method == "GET" and path == "/services":
            return self._json(200, {"services": self.services})
        if request.method == "GET" and path == "/countries":
            svc = request.url.params.get("service")
            return self._json(200, {"service": svc, "countries": self.countries.get(svc, [])})
        if request.method == "POST" and path == "/numbers":
            return self._buy(request)
        if request.method == "GET" and path == "/orders":
            rows = sorted(self.orders.values(), key=lambda o: -o["id"])
            return self._json(200, {"orders": [self.public(o) for o in rows[: int(request.url.params.get("limit", 40))]]})
        if path.startswith("/numbers/"):
            nh_id = int(path.split("/")[2])
            o = self.orders.get(nh_id)
            if o is None:
                return self._json(404, {"error": "not_found"})
            if request.method == "GET":
                return self._json(200, {"number": self.public(o)})
            if request.method == "DELETE":
                if self.cancel_lock:
                    return self._json(409, {"error": "cancel_locked", "seconds_remaining": self.cancel_lock})
                if self.cancel_code_first:
                    self.set_status(nh_id, "received", "999111")
                    return self._json(200, {"ok": False, "number": self.public(o), "reason": "code_received"})
                if o["status"] in ("pending", "waiting"):
                    self.set_status(nh_id, "canceled")
                    return self._json(200, {"ok": True, "number": self.public(o)})
                return self._json(200, {"ok": False, "number": self.public(o)})
        return self._json(404, {"error": "route_not_found"})

    def _buy(self, request: httpx.Request) -> httpx.Response:
        key = request.headers.get("idempotency-key")
        if not key:
            return self._json(400, {"error": "idempotency_key_required"})
        body = json.loads(request.content)
        raw = request.content
        if key in self.idem:
            prev_raw, prev = self.idem[key]
            if prev_raw != hash(raw):
                return self._json(409, {"error": "idempotency_conflict"})
            self._maybe_lose(request)
            return httpx.Response(prev["status"], json=prev["body"], headers={"Idempotency-Replayed": "true"})
        status, out = self._do_buy(body)
        self.idem[key] = (hash(raw), {"status": status, "body": out})
        self._maybe_lose(request)
        return self._json(status, out)

    def _maybe_lose(self, request) -> None:
        """The server acted, but the reply never arrives."""
        if self.lose_replies > 0:
            self.lose_replies -= 1
            raise httpx.ReadTimeout("lost reply", request=request)

    def _do_buy(self, body: dict) -> tuple[int, dict]:
        if self.fail_next_buy:
            code, self.fail_next_buy = self.fail_next_buy, None
            if code == "insufficient_funds":
                return 402, {"error": code}
            if code == "price_exceeded":
                return 409, {"error": code, "price": "0.45", "max_price": body["max_price"]}
            return 409, {"error": code}
        row = next((r for r in self.countries.get(body["service"], []) if r["country"] == body["country"]), None)
        if row is None:
            return 409, {"error": "sold_out"}
        price = Decimal(self.true_reserve.get((body["service"], body["country"]), row["price_max"]))
        if Decimal(body["max_price"]) < price:
            return 409, {"error": "price_exceeded", "price": f"{price:.2f}", "max_price": body["max_price"]}
        if self.wallet - self.held < price:
            return 402, {"error": "insufficient_funds"}
        self.held += price
        self.next_id += 1
        now = dt.datetime.now(UTC)
        o = {"id": self.next_id, "kind": "sms", "service": body["service"], "service_name": "WhatsApp",
             "country": body["country"], "country_name": row["name"], "phone": f"1555000{self.next_id}",
             "verification": "sms", "status": "waiting" if row["in_stock"] else "pending",
             "status_label": "", "price": f"{price:.2f}", "code": None, "codes": [], "operator": None,
             "created_at": iso(now), "expires_at": iso(now + dt.timedelta(minutes=20)),
             "cancel_available_at": iso(now + dt.timedelta(seconds=125))}
        if not row["in_stock"]:
            o["phone"] = None
        self.orders[o["id"]] = o
        return 201, {"number": self.public(o)}


# ─── Telegram ────────────────────────────────────────────────────────────────
class FakeSession(BaseSession):
    """Records every Bot API call and answers with plausible objects."""

    def __init__(self, bot_id: int = 777, username: str = "my_shop_bot"):
        super().__init__()
        self.requests: list = []
        self.bot_id = bot_id
        self.username = username
        self.msg_id = 100

    async def close(self) -> None:
        pass

    async def stream_content(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def make_request(self, bot, method, timeout=None):  # noqa: ANN001
        self.requests.append(method)
        if isinstance(method, GetMe):
            return User(id=self.bot_id, is_bot=True, first_name="My Shop", username=self.username)
        if isinstance(method, (SendMessage, EditMessageText)):
            self.msg_id += 1
            chat_id = method.chat_id
            return Message.model_validate({
                "message_id": method.message_id if isinstance(method, EditMessageText) and method.message_id else self.msg_id,
                "date": int(dt.datetime.now().timestamp()), "chat": {"id": chat_id, "type": "private"},
                "text": method.text}, context={"bot": bot})
        if isinstance(method, (AnswerCallbackQuery, DeleteMessage, SetMyCommands)):
            return True
        return True

    # helpers
    def texts(self) -> list[str]:
        return [m.text for m in self.requests if isinstance(m, (SendMessage, EditMessageText))]

    def last_text(self) -> str:
        t = self.texts()
        return t[-1] if t else ""

    def alerts(self) -> list[str]:
        return [m.text or "" for m in self.requests if isinstance(m, AnswerCallbackQuery)]

    def last_markup(self):
        for m in reversed(self.requests):
            if isinstance(m, (SendMessage, EditMessageText)):
                return m.reply_markup
        return None


def fake_bot(session: FakeSession) -> Bot:
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    return Bot("123456:" + "A" * 35, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
