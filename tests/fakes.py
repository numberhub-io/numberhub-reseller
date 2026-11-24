"""Test doubles: an in-memory NumberHub API (httpx MockTransport) and a fake
Telegram Bot API session for aiogram, so the real code runs end to end."""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import httpx
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (AnswerCallbackQuery, DeleteMessage, EditMessageText, GetMe, SendMessage, SendPhoto,
                             SetMyCommands)
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
        # Like the real server's layers in front of the purchase handler:
        self.rate_limit_next = 0                     # next N requests: 429 rate_limited
        self.then_rate_limit = 0                     # after the next lost reply, N requests get 429
        self.in_progress_next = 0                    # next N replays: 409 idempotency_in_progress
        self.scopes = {"catalog:read", "orders:read", "orders:write", "wallet:read"}
        # email codes (GET /email/..., POST /emails, GET and DELETE /emails/{id})
        self.email_has = True                        # False = an API without email codes
        self.email_sites_list = [{"site": "openai.com", "name": "OpenAI"},
                                 {"site": "accounts.google.com", "name": "Google"},
                                 {"site": "reddit.com", "name": "Reddit"}]
        self.email_domains = {
            "openai.com": [{"domain": "gmx.com", "price": "0.03", "in_stock": True, "delivery": "high"},
                           {"domain": "hotmail.com", "price": "0.05", "in_stock": True, "delivery": None},
                           {"domain": "yandex.com", "price": "0.01", "in_stock": True, "delivery": "low"},
                           {"domain": "mail.ru", "price": "0.02", "in_stock": False, "delivery": None}],
            "accounts.google.com": [{"domain": "outlook.com", "price": "0.04", "in_stock": True, "delivery": "high"}],
            "reddit.com": [],
        }
        self.email_bodies: list[dict] = []
        # proxy data packages (GET /proxy/packages, POST /proxies, GET /proxies/{id} and .../usage)
        self.proxy_enabled = True
        self.proxy_packages_list = [
            {"code": "p1", "gb": 1, "price": "0.49", "per_gb": "0.49", "save_percent": 0, "popular": False},
            {"code": "p5", "gb": 5, "price": "2.45", "per_gb": "0.49", "save_percent": 0, "popular": True},
            {"code": "p10", "gb": 10, "price": "4.90", "per_gb": "0.49", "save_percent": 0, "popular": False}]
        self.proxy_bodies: list[dict] = []
        self.proxy_usage_down = False                # GET /proxies/{id}/usage answers 502
        self.proxy_down = False                      # every GET /proxies/... answers 503
        # deposits into the wallet (POST /topups, GET /topups/{id})
        self.topups: dict[int, dict] = {}
        self.next_topup = 500
        self.topup_bodies: list[dict] = []
        self.topup_supports_reference = True         # False = an older API that ignores `reference`
        self.fail_next_topup: str | None = None      # an error code for the next POST /topups
        self.lose_topup_replies = 0                  # POST /topups that act, then "time out"
        self.has_methods_list = True                 # False = an API from before GET /topups/methods

    def _scope(self, method: str, path: str) -> str:
        if path == "/balance":
            return "wallet:read"
        if path in ("/services", "/countries") or path.startswith("/email/") or path.startswith("/proxy/"):
            return "catalog:read"
        if path.startswith("/topups"):
            return "orders:write" if method == "POST" else "wallet:read"
        return "orders:write" if method in ("POST", "DELETE") else "orders:read"

    # ── deposits ──
    # method id -> (network, short name, coin)
    NETWORKS = {"usdt_bsc": ("BNB Smart Chain (BEP20)", "BEP20", "USDT"), "usdt_tron": ("Tron (TRC20)", "TRC20", "USDT"),
                "btc": ("Bitcoin", "Bitcoin", "BTC"), "ltc": ("Litecoin", "Litecoin", "LTC"),
                "eth": ("Ethereum", "Ethereum", "ETH"), "eth_bsc": ("BNB Smart Chain (BEP20)", "BEP20", "ETH"),
                "usdc_base": ("Base", "Base", "USDC")}

    def topup_public(self, t: dict, reused: bool | None = None) -> dict:
        payable = t["status"] == "pending"
        out = {"id": t["id"], "status": t["status"], "amount": f"{t['amount']:.2f}",
               "credited": f"{t['credited']:.2f}" if t["credited"] is not None else None, "currency": "USD",
               "method": t["method"], "pay_currency": self.NETWORKS[t["method"]][2], "network": self.NETWORKS[t["method"]][0],
               "network_short": self.NETWORKS[t["method"]][1],
               "address": t["address"] if payable else None, "pay_amount": t["pay_amount"] if payable else None,
               "expires_at": iso(t["expires_at"]), "created_at": iso(t["created_at"]), "paid_at": iso(t["paid_at"]),
               "reference": t["reference"]}
        if reused is not None:
            out["reused"] = reused
        return out

    def pay_topup(self, topup_id: int, credited: str | None = None) -> None:
        """The customer's USDT arrived: the deposit is paid and the wallet has it."""
        t = self.topups[topup_id]
        t["status"], t["paid_at"] = "paid", dt.datetime.now(UTC)
        t["credited"] = Decimal(credited) if credited is not None else t["amount"]
        self.wallet += t["credited"]

    def owner_topup(self, amount: str) -> int:
        """A paid top-up the account owner made themselves (no reference)."""
        tid = self._new_topup(Decimal(amount), "usdt_bsc", None, 2880)
        self.pay_topup(tid)
        return tid

    def _new_topup(self, amount: Decimal, method: str, reference, lifetime: int) -> int:
        tid = self.next_topup
        self.next_topup += 1
        now = dt.datetime.now(UTC)
        self.topups[tid] = {"id": tid, "status": "pending", "amount": amount, "credited": None, "method": method,
                            "reference": reference, "lifetime": lifetime, "address": f"0xSHOPADDR{tid}",
                            "pay_amount": (f"{amount * Decimal('1.02'):.2f}" if self.NETWORKS[method][2] in ("USDT", "USDC")
                                           else "0.00031"),      # another coin: its own small number
                            "created_at": now, "paid_at": None,
                            "expires_at": now + dt.timedelta(minutes=lifetime)}
        return tid

    def _open_topup(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.topup_bodies.append(body)
        if self.fail_next_topup:
            code, self.fail_next_topup = self.fail_next_topup, None
            status = {"too_many_open_topups": 429, "topups_unavailable": 503, "topup_failed": 502}.get(code, 400)
            return self._json(status, {"error": code})
        amount = Decimal(str(body["amount"])).quantize(Decimal("0.01"))
        method = body.get("method")
        if method not in self.NETWORKS:
            return self._json(400, {"error": "unknown_method"})
        reference = body.get("reference") if self.topup_supports_reference else None
        lifetime = int(body.get("lifetime_minutes") or 2880)
        for t in self.topups.values():      # the same request again: the open deposit, never a second one
            if (t["status"] == "pending" and t["amount"] == amount and t["method"] == method
                    and t["reference"] == reference):
                return self._json(200, {"topup": self.topup_public(t, reused=True)})
        tid = self._new_topup(amount, method, reference, lifetime)
        if self.lose_topup_replies > 0:
            self.lose_topup_replies -= 1
            raise httpx.ReadTimeout("lost reply", request=request)
        return self._json(201, {"topup": self.topup_public(self.topups[tid], reused=False)})

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

    def email_public(self, o: dict) -> dict:
        """GET /emails/{id}: the order plus its `email` block."""
        mail = o.get("_mail") or {}
        return {**self.public(o), "email": {
            "address": o["phone"], "code": o["codes"][-1] if o["codes"] else None, "link": mail.get("link"),
            "links": [mail["link"]] if mail.get("link") else [], "message": mail.get("message")}}

    def mail_arrives(self, nh_id: int, code: str | None = None, link: str | None = None,
                     message: str | None = None) -> None:
        """The mailbox got its mail: a code, a link, or only text."""
        self.orders[nh_id]["_mail"] = {"link": link, "message": message}
        self.set_status(nh_id, "received", code)

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
        if self.rate_limit_next > 0:
            self.rate_limit_next -= 1
            return self._json(429, {"error": "rate_limited"})
        if request.headers.get("authorization") != f"Bearer {self.key}" or self.reject_key:
            return self._json(401, {"error": "invalid_api_key"})
        need = self._scope(request.method, path)
        if need not in self.scopes:
            return self._json(403, {"error": "insufficient_scope", "required_scope": need})
        if request.method == "GET" and path == "/balance":
            return self._json(200, {"balance": f"{self.wallet:.2f}", "available": f"{self.wallet - self.held:.2f}",
                                    "held": f"{self.held:.2f}", "currency": "USD"})
        if request.method == "GET" and path == "/services":
            return self._json(200, {"services": self.services})
        if request.method == "GET" and path == "/countries":
            svc = request.url.params.get("service")
            return self._json(200, {"service": svc, "countries": self.countries.get(svc, [])})
        if request.method == "POST" and path in ("/numbers", "/emails", "/proxies"):
            return self._buy(request)
        if request.method == "GET" and path == "/proxy/packages":
            return self._json(200, {"enabled": self.proxy_enabled, "validity_days": 30, "currency": "USD",
                                    "from_per_gb": "0.49",
                                    "packages": self.proxy_packages_list if self.proxy_enabled else []})
        if request.method == "GET" and path.startswith("/proxies/"):
            parts = path.split("/")
            o = self.orders.get(int(parts[2])) if parts[2].isdigit() else None
            if o is None or o.get("kind") != "proxy":
                return self._json(404, {"error": "not_found"})
            if self.proxy_down:
                return self._json(503, {"error": "unavailable"})
            if len(parts) > 3 and parts[3] == "usage":
                if self.proxy_usage_down:
                    return self._json(502, {"error": "usage_unavailable"})
                return self._json(200, {"proxy": self.proxy_public(o, usage=True)})
            return self._json(200, {"proxy": self.proxy_public(o)})
        if request.method == "GET" and path == "/email/sites":
            return self._json(200, {"sites": self.email_sites_list if self.email_has else []})
        if request.method == "GET" and path == "/email/suggest":
            q = (request.url.params.get("q") or "").lower()
            hits = [dict(s, known=True) for s in self.email_sites_list if q in s["site"] or q in s["name"].lower()]
            # a name the catalogue only knows as an app: its address is a guess
            hits += [dict(s, known=False) for s in getattr(self, "email_guesses", []) if q in s["name"].lower()]
            return self._json(200, {"suggestions": hits if self.email_has else []})
        if request.method == "GET" and path.startswith("/email/sites/") and path.endswith("/domains"):
            from urllib.parse import unquote
            site = unquote(path.split("/")[3])
            return self._json(200, {"site": site, "site_name": site, "domains": self.email_domains.get(site, [])})
        if path.startswith("/emails/"):
            tail = path.split("/")[2]
            o = self.orders.get(int(tail)) if tail.isdigit() else None
            if o is None or o.get("kind") != "email":
                return self._json(404, {"error": "not_found"})
            if request.method == "GET":
                return self._json(200, {"email": self.email_public(o)})
            if request.method == "DELETE":
                if o["status"] != "waiting":
                    return self._json(409, {"error": "not_cancelable", "order_status": o["status"]})
                if self.cancel_lock:
                    return self._json(409, {"error": "cancel_locked", "seconds_remaining": self.cancel_lock})
                if self.cancel_code_first:
                    self.mail_arrives(o["id"], "999111")
                    return self._json(200, {"ok": False, "email": self.email_public(o), "reason": "code_received"})
                self.set_status(o["id"], "canceled")
                return self._json(200, {"ok": True, "email": self.email_public(o)})
        if request.method == "POST" and path == "/topups":
            return self._open_topup(request)
        if request.method == "GET" and path == "/topups/methods" and self.has_methods_list:
            return self._json(200, {"methods": [
                {"id": mid, "coin": coin, "network": net, "network_short": short, "partial_credit": coin in ("USDT", "USDC"),
                 "min_amount": "25.00" if mid == "btc" else "1.00"}
                for mid, (net, short, coin) in self.NETWORKS.items()]})
        if request.method == "GET" and path.startswith("/topups/"):
            tid = path.split("/")[2]
            t = self.topups.get(int(tid)) if tid.isdigit() else None
            if t is None:
                return self._json(404, {"error": "not_found"})
            return self._json(200, {"topup": self.topup_public(t)})
        if request.method == "GET" and path == "/orders":
            rows = sorted(self.orders.values(), key=lambda o: -o["id"])
            limit = max(1, min(100, int(request.url.params.get("limit", 40))))   # the server clamps to 100
            return self._json(200, {"orders": [self.public(o) for o in rows[:limit]]})
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
            if self.in_progress_next > 0:
                self.in_progress_next -= 1
                return self._json(409, {"error": "idempotency_in_progress"}, {"Retry-After": "2"})
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
            self.rate_limit_next += self.then_rate_limit     # and the retries then hit 429
            self.then_rate_limit = 0
            raise httpx.ReadTimeout("lost reply", request=request)

    def proxy_public(self, o: dict, usage: bool = False) -> dict:
        """GET /proxies/{id}: the order plus its `proxy` block (with `usage` from .../usage)."""
        login, pw, ip = o["phone"], o["_password"], "203.0.113.7"
        used, limit = int(o.get("_used", 0)), o["_gb"] * 1024 ** 3
        size = lambda n: f"{n / 1024 ** 3:.2f} GB"   # noqa: E731
        return {**self.public(o), "proxy": {
            "host": "proxy.wallet.example", "ip": ip, "http_port": 8080, "socks_port": 1080,
            "username": login, "password": pw, "gb": o["_gb"], "validity_days": 30, "country": None,
            "lines": {"any": f"{ip}:8080:{login}:{pw}", "country": f"{ip}:8080:{login}-country-us:{pw}",
                      "sticky": f"{ip}:8080:{login}-country-us-sid-k3x9w2:{pw}",
                      "socks": f"{ip}:1080:{login}-country-us:{pw}"},
            "telegram_url": f"https://t.me/socks?server=proxy.wallet.example&port=1080&user={login}&pass={pw}",
            "setup": {},
            "usage": ({"used_bytes": used, "limit_bytes": limit, "left_bytes": limit - used, "used": size(used),
                       "total": size(limit), "left": size(limit - used), "left_pct": 100} if usage else None)}}

    def _do_buy_proxy(self, body: dict) -> tuple[int, dict]:
        self.proxy_bodies.append(body)
        if self.fail_next_buy:
            code, self.fail_next_buy = self.fail_next_buy, None
            if code == "price_exceeded":
                return 409, {"error": code, "price": "3.00", "max_price": body["max_price"]}
            return (402 if code == "insufficient_funds" else 409), {"error": code}
        pkg = (next((p for p in self.proxy_packages_list if p["gb"] == body.get("gb")), None)
               if self.proxy_enabled else None)
        if pkg is None:
            return 404, {"error": "unknown_package"}
        price = Decimal(pkg["price"])
        if Decimal(body["max_price"]) < price:
            return 409, {"error": "price_exceeded", "price": f"{price:.2f}", "max_price": body["max_price"]}
        if self.wallet - self.held < price:
            return 402, {"error": "insufficient_funds"}
        self.wallet -= price                         # paid when bought: nothing is held
        self.next_id += 1
        now = dt.datetime.now(UTC)
        o = {"id": self.next_id, "kind": "proxy", "service": "proxy",
             "service_name": f"{pkg['gb']} GB residential proxy", "country": "", "country_name": "Worldwide",
             "phone": f"nh{self.next_id}x", "verification": "sms", "status": "received", "status_label": "",
             "price": f"{price:.2f}", "code": None, "codes": [], "operator": None, "created_at": iso(now),
             "expires_at": iso(now + dt.timedelta(days=30)), "cancel_available_at": None,
             "_charged": True, "_gb": pkg["gb"], "_password": f"Pw{self.next_id}secret", "_used": 0}
        self.orders[o["id"]] = o
        return 201, {"proxy": self.proxy_public(o)}

    def _do_buy_email(self, body: dict) -> tuple[int, dict]:
        self.email_bodies.append(body)
        if self.fail_next_buy:
            code, self.fail_next_buy = self.fail_next_buy, None
            return (402 if code == "insufficient_funds" else 409), {"error": code}
        site, domain = body.get("site"), body.get("domain")
        row = next((d for d in self.email_domains.get(site, []) if d["domain"] == domain and d["in_stock"]), None)
        if row is None:
            return 409, {"error": "sold_out"}
        price = Decimal(row["price"])
        if Decimal(body["max_price"]) < price:
            return 409, {"error": "price_exceeded", "price": f"{price:.2f}", "max_price": body["max_price"]}
        if self.wallet - self.held < price:
            return 402, {"error": "insufficient_funds"}
        self.held += price
        self.next_id += 1
        now = dt.datetime.now(UTC)
        name = next((s["name"] for s in self.email_sites_list if s["site"] == site), site)
        o = {"id": self.next_id, "kind": "email", "service": site, "service_name": name, "country": domain,
             "country_name": domain, "phone": f"box{self.next_id}@{domain}", "verification": "sms",
             "status": "waiting", "status_label": "", "price": f"{price:.2f}", "code": None, "codes": [],
             "operator": None, "created_at": iso(now), "expires_at": iso(now + dt.timedelta(minutes=20)),
             "cancel_available_at": iso(now + dt.timedelta(seconds=125)), "_mail": {}}
        self.orders[o["id"]] = o
        return 201, {"email": self.email_public(o)}

    def _do_buy(self, body: dict) -> tuple[int, dict]:
        if "site" in body:
            return self._do_buy_email(body)
        if "gb" in body:
            return self._do_buy_proxy(body)
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


class FakeFx:
    """The two public sources of the dollar-to-rupiah rate (app/fx.py)."""

    def __init__(self):
        self.first, self.second = 18195.480115, 18190.1
        self.down: set[str] = set()
        self.calls: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.calls.append(host)
        if host in self.down:
            return httpx.Response(503, text="down")
        if host == "open.er-api.com":
            return httpx.Response(200, json={"result": "success", "base_code": "USD", "rates": {"IDR": self.first, "EUR": 0.9}})
        if host == "cdn.jsdelivr.net":
            return httpx.Response(200, json={"date": "2026-11-23", "usd": {"idr": self.second, "eur": 0.9}})
        return httpx.Response(404, text="?")


# ─── Telegram ────────────────────────────────────────────────────────────────
class FakeSession(BaseSession):
    """Records every Bot API call and answers with plausible objects."""

    def __init__(self, bot_id: int = 777, username: str = "my_shop_bot"):
        super().__init__()
        self.requests: list = []
        self.bot_id = bot_id
        self.username = username
        self.msg_id = 100
        self.fail_get_me = 0          # next N getMe calls fail (a network blip at boot)
        self.fail_send = 0            # next N sendMessage calls hit a flood limit

    async def close(self) -> None:
        pass

    async def stream_content(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def make_request(self, bot, method, timeout=None):  # noqa: ANN001
        self.requests.append(method)
        if isinstance(method, GetMe) and self.fail_get_me > 0:
            self.fail_get_me -= 1
            from aiogram.exceptions import TelegramNetworkError
            raise TelegramNetworkError(method=method, message="boot blip")
        if isinstance(method, SendMessage) and self.fail_send > 0:
            self.fail_send -= 1
            from aiogram.exceptions import TelegramRetryAfter
            raise TelegramRetryAfter(method=method, message="flood", retry_after=3)
        if isinstance(method, GetMe):
            return User(id=self.bot_id, is_bot=True, first_name="My Shop", username=self.username)
        if isinstance(method, (SendMessage, EditMessageText)):
            self.msg_id += 1
            chat_id = method.chat_id
            return Message.model_validate({
                "message_id": method.message_id if isinstance(method, EditMessageText) and method.message_id else self.msg_id,
                "date": int(dt.datetime.now().timestamp()), "chat": {"id": chat_id, "type": "private"},
                "text": method.text}, context={"bot": bot})
        if isinstance(method, SendPhoto):
            self.msg_id += 1
            return Message.model_validate({
                "message_id": self.msg_id, "date": int(dt.datetime.now().timestamp()),
                "chat": {"id": method.chat_id, "type": "private"}, "caption": method.caption,
                "photo": [{"file_id": "f", "file_unique_id": "u", "width": 8, "height": 8}]}, context={"bot": bot})
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


# ─── payment gateways (app/payments.py) ──────────────────────────────────────
class FakeGateways:
    """OxaPay (api.oxapay.com/v1) and Crypto Pay (pay.crypt.bot/api) for one shop
    owner's own account. Tests set invoice states and knobs, then let the real
    payments code talk to them through an httpx.MockTransport."""

    def __init__(self, ox_key: str = "OXA-GOOD-KEY-123", cb_token: str = "12345:CRYPTOPAYTOKEN"):
        self.ox_key, self.cb_token = ox_key, cb_token
        self.ox: dict[str, dict] = {}        # track_id -> {"status", "amount", "sandbox", ...}
        self.cb: dict[int, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.refuse_create = False
        self.next = 1
        # KaseraPay (pay.kasera.id/v1): a live key and a test key of one account
        self.ks_key, self.ks_test_key = "kp_live_GOODKEY1234567890", "kp_test_GOODKEY1234567890"
        self.ks: dict[str, dict] = {}
        self.ks_idem: dict[str, str] = {}
        self.ks_bodies: list[dict] = []
        self.ks_naive = False              # answer the expiry without a time zone

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        self.calls.append((host, path))
        if host == "api.oxapay.com":
            if request.headers.get("merchant_api_key") != self.ox_key:
                return httpx.Response(400, json={"data": {}, "message": "", "error": {
                    "type": "invalid_key", "key": "merchant_api_key", "message": "Invalid merchant API key"},
                    "status": 400})
            if request.method == "POST" and path == "/v1/payment/invoice":
                body = json.loads(request.content)
                if self.refuse_create:
                    return httpx.Response(400, json={"error": {"message": "Merchant is blocked"}, "status": 400})
                track = f"T{self.next}"
                self.next += 1
                self.ox[track] = {"status": "Waiting", "amount": body["amount"], "sandbox": body.get("sandbox"),
                                  "fee_paid_by_payer": body.get("fee_paid_by_payer"), "order_id": body["order_id"]}
                exp = int((dt.datetime.now(UTC) + dt.timedelta(minutes=body["lifetime"])).timestamp())
                return httpx.Response(200, json={"data": {"track_id": track, "payment_url":
                                                          f"https://pay.oxapay.com/m1/{track}", "expired_at": exp},
                                                 "message": "ok", "error": {}, "status": 200})
            if request.method == "GET" and path.startswith("/v1/payment/"):
                track = path.rsplit("/", 1)[1]
                inv = self.ox.get(track)
                if inv is None:
                    return httpx.Response(404, json={"error": {"message": "not found"}, "status": 404})
                return httpx.Response(200, json={"data": {"track_id": track, "status": inv["status"]},
                                                 "message": "ok", "error": {}, "status": 200})
        if host == "pay.kasera.id":
            auth = request.headers.get("authorization", "")
            if auth not in (f"Bearer {self.ks_key}", f"Bearer {self.ks_test_key}"):
                return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "Invalid API key"}})
            if request.method == "POST" and path == "/v1/transactions":
                body = json.loads(request.content)
                self.ks_bodies.append(body)
                if self.refuse_create:
                    return httpx.Response(429, json={"error": {"code": "unverified_count_cap",
                                                               "message": "Unverified accounts can create 10 payments"}})
                idem = request.headers.get("idempotency-key") or f"none-{self.next}"
                if idem not in self.ks_idem:
                    tid = f"payreq_{self.next}"
                    self.next += 1
                    # The real gateway answers in Jakarta time: "2026-11-24T08:15:23+07:00".
                    wib = dt.timezone(dt.timedelta(hours=7))
                    until = (dt.datetime.now(UTC) + dt.timedelta(minutes=body.get("expires_in_minutes", 60))
                             ).astimezone(wib).replace(microsecond=0)
                    made = dt.datetime.now(wib).replace(microsecond=0)
                    if self.ks_naive:
                        until = until.replace(tzinfo=None)
                    self.ks[tid] = {"id": tid, "status": "pending", "amount": body["amount"], "currency": "IDR",
                                    "livemode": auth.endswith(self.ks_key), "payment_method": "qris",
                                    "payment": {"type": "qr", "qr_string": f"00020101021226670016ID.CO.QRIS.WWW{tid}"},
                                    "expires_at": until.isoformat(), "created_at": made.isoformat(),
                                    "paid_at": None, "fee": 285, "net": body["amount"] - 285,
                                    "merchant_ref": body.get("merchant_ref"), "external_id": body.get("external_id")}
                    self.ks_idem[idem] = tid
                return httpx.Response(201, json=self.ks[self.ks_idem[idem]])
            if request.method == "GET" and path.startswith("/v1/transactions/"):
                t = self.ks.get(path.rsplit("/", 1)[1])
                if t is None:
                    return httpx.Response(404, json={"error": {"code": "not_found", "message": "No such payment"}})
                return httpx.Response(200, json=t)
        if host == "pay.crypt.bot":
            if request.headers.get("crypto-pay-api-token") != self.cb_token:
                return httpx.Response(401, json={"ok": False, "error": {"code": 401, "name": "UNAUTHORIZED"}})
            body = json.loads(request.content or b"{}")
            method = path.rsplit("/", 1)[1]
            if method == "getMe":
                return httpx.Response(200, json={"ok": True, "result": {"app_id": 9, "name": "My shop app"}})
            if method == "createInvoice":
                if self.refuse_create:
                    return httpx.Response(400, json={"ok": False, "error": {"code": 400, "name": "APP_BLOCKED"}})
                iid = self.next
                self.next += 1
                self.cb[iid] = {"status": "active", **body}
                return httpx.Response(200, json={"ok": True, "result": {
                    "invoice_id": iid, "status": "active", "bot_invoice_url": f"https://t.me/CryptoBot?start=IV{iid}"}})
            if method == "getInvoices":
                ids = [int(x) for x in str(body.get("invoice_ids", "")).split(",") if x]
                return httpx.Response(200, json={"ok": True, "result": {"items": [
                    {"invoice_id": i, "status": self.cb[i]["status"]} for i in ids if i in self.cb]}})
        return httpx.Response(404, json={"error": "no such route"})
