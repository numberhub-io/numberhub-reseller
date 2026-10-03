"""Hosted shops: NumberHub's own bot creates a shop here from just a bot token.

The customer never handles an API key: NumberHub mints one for their account
(so every number their shop sells is paid from their NumberHub wallet, only
when its code arrives) and hands it over with the token. This listener is the
only way in: bound to 127.0.0.1, every request carries the shared
PROVISION_SECRET, and it does exactly what the builder bot's create / reconnect /
pause / resume do, with the same checks.

    POST /internal/shops                {owner_id, owner_username?, token, api_key, markup_pct?}
    GET  /internal/shops?owner_id=N     the owner's shops with 7-day numbers
    POST /internal/shops/{id}/status    {owner_id, status: "active" | "disabled"}
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from decimal import Decimal, InvalidOperation

from aiohttp import web
from sqlalchemy.exc import IntegrityError

from app import crypto, repo, runtime
from app.bots.builder import KEY_RE, MAX_BOTS_PER_OWNER, TOKEN_RE, _an_open_order
from app.config import settings
from app.models import Reseller
from app.numberhub import NumberHub, NumberHubError, dec

log = logging.getLogger(__name__)
_locks: dict[int, asyncio.Lock] = {}


def _err(code: str, status: int, **extra) -> web.Response:
    return web.json_response({"error": code, **extra}, status=status)


@web.middleware
async def _auth(request: web.Request, handler):
    sent = request.headers.get("X-Provision-Secret", "")
    if not settings.provision_secret or not hmac.compare_digest(sent.encode(), settings.provision_secret.encode()):
        return _err("forbidden", 403)
    return await handler(request)


def _owner(value) -> int | None:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return v if 0 < v < 2 ** 53 else None


def _markup(value) -> Decimal | None:
    if value in (None, ""):
        return settings.default_markup_pct
    try:
        v = Decimal(str(value).replace("%", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not v.is_finite() or v < 0 or v > settings.max_markup_pct:
        return None
    return v.quantize(Decimal("0.01"))


async def _key_problem(key: str, open_nh_id: int | None) -> tuple[dict | None, str | None]:
    """The same checks as the builder's (wallet, catalog, orders, and the SAME
    account as the shop's open orders), as error codes."""
    cli = NumberHub(key)
    try:
        bal = await cli.balance()
        await cli.services()
        await cli.orders(limit=1)
        if open_nh_id is not None:
            await cli.number(open_nh_id)
        return bal, None
    except NumberHubError as exc:
        if exc.code == "insufficient_scope":
            return None, "key_scope"
        if exc.code == "ip_not_allowed":
            return None, "key_ip"
        if exc.status == 404 and open_nh_id is not None:
            return None, "other_account"
        if exc.status in (401, 403):
            return None, "key_rejected"
        return None, "numberhub_unavailable"
    finally:
        await cli.close()


async def create_shop(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return _err("bad_request", 400)
    if not isinstance(data, dict):
        return _err("bad_request", 400)
    owner_id = _owner(data.get("owner_id"))
    token = str(data.get("token") or "").strip()
    key = str(data.get("api_key") or "").strip()
    markup = _markup(data.get("markup_pct"))
    if owner_id is None or markup is None:
        return _err("bad_request", 400)
    if not TOKEN_RE.match(token):
        return _err("bad_token", 400)
    if not KEY_RE.match(key):
        return _err("bad_key", 400)
    probe = runtime.make_bot(token)
    try:
        me = await probe.get_me()
    except Exception:  # noqa: BLE001
        return _err("token_rejected", 400)
    finally:
        await probe.session.close()
    lock = _locks.setdefault(me.id, asyncio.Lock())
    async with lock:
        existing = await repo.get_reseller_by_bot_id(me.id)
        if existing is not None and existing.owner_id != owner_id:
            return _err("taken", 409)
        if existing is not None and existing.status == Reseller.SUSPENDED:
            return _err("suspended", 409)
        if existing is None and len(await repo.list_resellers(owner_id=owner_id)) >= MAX_BOTS_PER_OWNER:
            return _err("too_many", 409, max=MAX_BOTS_PER_OWNER)
        bal, problem = await _key_problem(key, await _an_open_order(existing.id) if existing else None)
        if problem:
            return _err(problem, 502 if problem == "numberhub_unavailable" else 409)
        if existing is not None:
            # Same bot again (a new token after /revoke, or a fresh key): the row is
            # updated in place so its customers, balances and orders stay.
            status = Reseller.DISABLED if existing.status == Reseller.DISABLED else Reseller.ACTIVE
            await repo.update_reseller(existing.id, bot_token_enc=crypto.encrypt(token), bot_username=me.username,
                                       bot_title=me.full_name, api_key_enc=crypto.encrypt(key),
                                       api_key_hint=key[-4:], status=status)
            await runtime.restart(existing.id)
            log.info("hosted shop %s reconnected for owner %s", existing.id, owner_id)
            return web.json_response({"shop": _shop_json(await repo.get_reseller(existing.id)),
                                      "reconnected": True, "wallet": str(dec(bal.get("available")))})
        username = str(data.get("owner_username") or "").strip().lstrip("@")
        try:
            reseller = await repo.create_reseller(
                owner_id=owner_id, bot_token_enc=crypto.encrypt(token), bot_id=me.id, bot_username=me.username,
                bot_title=me.full_name, api_key_enc=crypto.encrypt(key), api_key_hint=key[-4:],
                markup_pct=markup, support_contact=f"@{username}" if username else None, status=Reseller.ACTIVE)
        except IntegrityError:
            return _err("taken", 409)
        await runtime.start(reseller)
        log.info("hosted shop %s (@%s) created for owner %s, commission %s%%",
                 reseller.id, reseller.bot_username, owner_id, markup)
        return web.json_response({"shop": _shop_json(reseller), "reconnected": False,
                                  "wallet": str(dec(bal.get("available")))}, status=201)


def _shop_json(r: Reseller, week: dict | None = None) -> dict:
    out = {"id": r.id, "bot_username": r.bot_username, "status": r.status,
           "markup_pct": str(r.markup_pct)}
    if week is not None:
        out.update(members=week["members"], orders_7d=week["orders"],
                   profit_7d=str(dec(week["sales"]) - dec(week["cost"])))
    return out


async def list_shops(request: web.Request) -> web.Response:
    owner_id = _owner(request.query.get("owner_id"))
    if owner_id is None:
        return _err("bad_request", 400)
    rows = await repo.list_resellers(owner_id=owner_id)
    return web.json_response({"shops": [_shop_json(r, await repo.stats(r.id, 7)) for r in rows]})


async def set_status(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        rid = int(request.match_info["id"])
    except Exception:  # noqa: BLE001
        return _err("bad_request", 400)
    owner_id = _owner((data or {}).get("owner_id")) if isinstance(data, dict) else None
    want = (data or {}).get("status") if isinstance(data, dict) else None
    reseller = await repo.get_reseller(rid)
    if owner_id is None or reseller is None or reseller.owner_id != owner_id:
        return _err("not_found", 404)
    if reseller.status == Reseller.SUSPENDED:
        return _err("suspended", 409)
    if want == "disabled":
        # Paused: the bot keeps running, customers see "paused", open orders finish.
        await repo.update_reseller(rid, status=Reseller.DISABLED)
    elif want == "active":
        if reseller.status == Reseller.KEY_INVALID:
            return _err("key_invalid", 409)
        await repo.update_reseller(rid, status=Reseller.ACTIVE)
        await runtime.start(await repo.get_reseller(rid))
    else:
        return _err("bad_request", 400)
    return web.json_response({"shop": _shop_json(await repo.get_reseller(rid))})


def build_app() -> web.Application:
    app = web.Application(middlewares=[_auth], client_max_size=16 * 1024)
    app.router.add_post("/internal/shops", create_shop)
    app.router.add_get("/internal/shops", list_shops)
    app.router.add_post("/internal/shops/{id}/status", set_status)
    return app


async def start_server() -> web.AppRunner | None:
    """Listen on 127.0.0.1 only, and only when a secret is configured."""
    if not settings.provision_secret:
        return None
    if len(settings.provision_secret) < 32:
        log.error("PROVISION_SECRET is shorter than 32 characters: hosted shops are off")
        return None
    runner = web.AppRunner(build_app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", settings.provision_port).start()
    log.info("hosted-shop provisioning listening on 127.0.0.1:%s", settings.provision_port)
    return runner
