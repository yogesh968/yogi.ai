"""FastAPI backend: dashboard API, Vobiz telephony webhooks + media WebSocket.

Always binds to 127.0.0.1. In the cloud, Caddy terminates HTTPS and proxies to it.
Run: python -m app.server   (or `python run.py` to start server + worker together)
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import auth, db, repo
from .agent import CallAgent, CallOptions
from .audio.devices import AudioDeviceError
from .audio.recorder import finalize_orphan
from .config import GEMINI_VOICES, get_settings
from .events import bus
from .migrate import migrate
from .phone import same_number, to_e164
from .transports.base import AudioTransport
from .transports.local import local_audio_available
from .vobiz import VobizError, busy_xml, make_call, stream_xml

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
PENDING_TTL_S = 300


# ---------------------------------------------------------------- live call manager
@dataclass
class Live:
    key: str
    agent: CallAgent
    task: asyncio.Task[int | None] | None = None


@dataclass
class Pending:
    opts: CallOptions
    created: float = field(default_factory=time.monotonic)


class CallManager:
    def __init__(self) -> None:
        self.calls: dict[str, Live] = {}
        self.pending: dict[str, Pending] = {}       # provider CallUUID -> context (set by answer webhook)
        self.outbound: dict[str, Pending] = {}      # our token -> context (set when we dial out)
        self.last_error = ""

    def active_count(self) -> int:
        return sum(1 for c in self.calls.values() if c.agent.active)

    def _gc(self) -> None:
        now = time.monotonic()
        for d in (self.pending, self.outbound):
            for k in [k for k, p in d.items() if now - p.created > PENDING_TTL_S]:
                d.pop(k, None)

    async def run(self, opts: CallOptions, transport: AudioTransport) -> int | None:
        """Run a call to completion (used directly by the Vobiz WebSocket handler)."""
        key = uuid.uuid4().hex[:12]
        live = Live(key, CallAgent(opts, transport))
        live.agent.key = key
        self.calls[key] = live
        try:
            return await live.agent.run()
        except Exception as e:
            log.exception("call crashed")
            self.last_error = f"Call failed: {e}"
            bus.publish({"type": "status", "state": "error", "message": self.last_error, "key": key})
            return None
        finally:
            await asyncio.sleep(0)  # let final events flush
            self.calls.pop(key, None)
            bus.publish({"type": "call_removed", "key": key})

    def start_background(self, opts: CallOptions, transport: AudioTransport) -> None:
        asyncio.create_task(self.run(opts, transport), name="call")

    def find(self, key: str) -> Live | None:
        return self.calls.get(key)

    def snapshot(self) -> list[dict[str, Any]]:
        return [{"key": k, **c.agent.snapshot()} for k, c in self.calls.items()]

    async def shutdown(self) -> None:
        for c in self.calls.values():
            c.agent.request_stop("server_shutdown")
        deadline = time.monotonic() + 20
        while self.calls and time.monotonic() < deadline:
            await asyncio.sleep(0.2)


manager = CallManager()


async def recover_crashed_calls() -> int:
    """Calls stuck in 'live' (server crashed mid-call): rebuild audio, save transcript, queue processing."""
    stuck = await repo.recover_stuck_calls()
    for c in stuck:
        path = None
        if c["recording_path"]:
            p = await asyncio.to_thread(finalize_orphan, Path(c["recording_path"]))
            path = str(p) if p else None
        await repo.finish_call(c["id"], end_reason="recovered_after_crash", recording_path=path,
                               transcript_text=await repo.live_transcript_text(c["id"]),
                               error="Server stopped during the call; recovered on restart.")
        log.warning("recovered call %s after crash", c["id"])
    return len(stuck)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    bus.bind_loop(asyncio.get_running_loop())
    try:
        await db.open_pool()
    except Exception as e:
        raise SystemExit(db.describe_db_error(e)) from None
    await migrate()
    n = await recover_crashed_calls()
    if n:
        log.warning("recovered %d call(s) left live by a crash", n)
    s = get_settings()
    if s.vobiz_enabled:
        log.info("Vobiz answer URL: %s", vobiz_url("answer"))
    yield
    await manager.shutdown()
    await db.close_pool()


app = FastAPI(title="Personal Call Assistant", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def vobiz_url(path: str, ws: bool = False) -> str:
    s = get_settings()
    base = s.public_base_url.rstrip("/")
    if ws:
        base = "wss://" + base.split("://", 1)[1]
    return f"{base}/vobiz/{s.vobiz_webhook_secret}/{path}"


# ---------------------------------------------------------------- security middleware
PUBLIC_PATHS = ("/login", "/api/login", "/healthz", "/static/login.css", "/static/login.js", "/vobiz/")


def _allowed_hosts() -> set[str]:
    s = get_settings()
    return {"127.0.0.1", "localhost", "::1"} | ({s.public_host} if s.public_host else set())


@app.middleware("http")
async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    if host not in _allowed_hosts():  # blocks DNS-rebinding and stray scanners
        return JSONResponse({"detail": "forbidden host"}, status_code=403)
    path = request.url.path
    if not path.startswith(PUBLIC_PATHS) and not auth.is_authenticated(request.cookies):
        if path.startswith("/api/") or path.startswith("/ws"):
            return JSONResponse({"detail": "Please log in"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    # CSRF: SameSite=Strict cookie + a custom header that cross-site forms cannot send.
    if path.startswith("/api/") and request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "fetch":
        return JSONResponse({"detail": "missing X-Requested-With"}, status_code=403)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = (
        f"default-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; "
        f"connect-src 'self' ws://{host}:* wss://{host}; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    if request.headers.get("x-forwarded-proto") == "https":
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error")
    msg = db.describe_db_error(exc) if "psycopg" in type(exc).__module__ else "Internal error, see server log."
    return JSONResponse({"detail": msg}, status_code=500)


# ---------------------------------------------------------------- schemas
class TestCall(BaseModel):
    direction: Literal["incoming", "outgoing"] = "incoming"
    contact_name: str = Field("", max_length=200)
    purpose: str = Field("", max_length=1000)


class OutboundCall(BaseModel):
    to: str = Field(..., min_length=6, max_length=32, pattern=r"^[0-9+\-() ]+$")
    contact_name: str = Field("", max_length=200)
    purpose: str = Field(..., min_length=3, max_length=1000)


class SettingsIn(BaseModel):
    style_rules: str | None = Field(None, max_length=8000)
    voice: str | None = None
    recording_enabled: bool | None = None
    silence_timeout_s: int | None = Field(None, ge=5, le=3600)

    @field_validator("voice")
    @classmethod
    def _voice(cls, v: str | None) -> str | None:
        if v is not None and v not in GEMINI_VOICES:
            raise ValueError("unknown voice")
        return v


class FactIn(BaseModel):
    fact: str = Field(..., min_length=1, max_length=1000)
    contact_name: str = Field("", max_length=200)


class ContactIn(BaseModel):
    phone: str = Field(..., min_length=6, max_length=32, pattern=r"^[0-9+\-() ]+$")
    name: str = Field(..., min_length=1, max_length=200)


class ActionPatch(BaseModel):
    done: bool


class LoginIn(BaseModel):
    password: str = Field(..., max_length=200)


# ---------------------------------------------------------------- pages + auth
@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/login", include_in_schema=False)
async def login_page() -> Response:
    if not auth.auth_required():
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC / "login.html", headers={"Cache-Control": "no-cache"})


@app.post("/api/login")
async def login(body: LoginIn, request: Request) -> JSONResponse:
    if not auth.auth_required():
        return JSONResponse({"ok": True})
    ip = auth.client_ip(request)
    if auth.rate_limited(ip):
        raise HTTPException(429, "Too many attempts. Try again in 10 minutes.")
    if not auth.check_password(ip, body.password):
        await asyncio.sleep(0.8)  # slow down guessing
        raise HTTPException(401, "Wrong password")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(auth.COOKIE, auth.make_session(), max_age=auth.SESSION_DAYS * 86400, httponly=True,
                    secure=request.headers.get("x-forwarded-proto") == "https", samesite="strict", path="/")
    return resp


@app.post("/api/logout")
async def logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "active_calls": manager.active_count()}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------------------------------------------------------------- Vobiz telephony
def _xml(body: str) -> Response:
    return Response(content=body, media_type="application/xml")


async def _params(request: Request) -> dict[str, str]:
    p = dict(request.query_params)
    if request.method == "POST":
        try:
            p.update({k: str(v) for k, v in (await request.form()).items()})
        except Exception:
            pass
    return p


@app.api_route("/vobiz/{secret}/answer", methods=["GET", "POST"], include_in_schema=False)
async def vobiz_answer(secret: str, request: Request) -> Response:
    if not auth.check_webhook_secret(secret):
        raise HTTPException(404)
    s = get_settings()
    p = await _params(request)
    call_uuid = p.get("CallUUID", "")
    if p.get("Event") == "Hangup":  # hangup callbacks default to the answer URL if none is set
        await repo.set_hangup_cause(call_uuid, p.get("HangupCause", ""))
        return _xml("<Response/>")
    manager._gc()
    token = p.get("out", "")
    if token and token in manager.outbound:
        opts = manager.outbound.pop(token).opts
        opts.provider_call_id = call_uuid
    else:
        caller = to_e164(p.get("From", ""))
        if manager.active_count() >= s.max_concurrent_calls:
            log.warning("rejecting call from %s: %d calls already active", caller, manager.active_count())
            return _xml(busy_xml())
        name = await repo.contact_name_for(caller)
        if not name and caller and same_number(caller, s.owner_phone):
            name = s.owner_name  # you called your own assistant
        opts = CallOptions(mode="phone", direction="incoming", contact_name=name, contact_phone=caller,
                           provider_call_id=call_uuid)
        log.info("incoming call %s from %s (forwarded from %s)", call_uuid, caller or "unknown", p.get("ForwardedFrom", "-"))
    manager.pending[call_uuid] = Pending(opts)
    return _xml(stream_xml(vobiz_url("ws", ws=True), vobiz_url("stream-status")))


@app.api_route("/vobiz/{secret}/hangup", methods=["GET", "POST"], include_in_schema=False)
async def vobiz_hangup(secret: str, request: Request) -> Response:
    if not auth.check_webhook_secret(secret):
        raise HTTPException(404)
    p = await _params(request)
    log.info("hangup %s cause=%s duration=%s", p.get("CallUUID"), p.get("HangupCause"), p.get("Duration"))
    if p.get("CallUUID"):
        await repo.set_hangup_cause(p["CallUUID"], p.get("HangupCause", ""))
    return Response(status_code=200)


@app.api_route("/vobiz/{secret}/stream-status", methods=["GET", "POST"], include_in_schema=False)
async def vobiz_stream_status(secret: str, request: Request) -> Response:
    if not auth.check_webhook_secret(secret):
        raise HTTPException(404)
    p = await _params(request)
    if p.get("Error"):
        log.warning("stream %s: %s %s", p.get("CallUUID"), p.get("Event"), p.get("Error"))
    return Response(status_code=200)


@app.websocket("/vobiz/{secret}/ws")
async def vobiz_ws(websocket: WebSocket, secret: str) -> None:
    if not auth.check_webhook_secret(secret):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    from .transports.vobiz import VobizTransport
    try:
        start: dict[str, Any] = {}
        async with asyncio.timeout(10):
            while not start:
                msg = await websocket.receive_json()
                if msg.get("event") == "start":
                    start = msg.get("start") or {}
    except (TimeoutError, WebSocketDisconnect, ValueError):
        log.warning("Vobiz stream closed before 'start'")
        return
    call_uuid = start.get("callId", "")
    pending = manager.pending.pop(call_uuid, None)
    opts = pending.opts if pending else CallOptions(mode="phone", direction="incoming", provider_call_id=call_uuid)
    await manager.run(opts, VobizTransport(websocket, start))


# ---------------------------------------------------------------- dashboard API
@app.get("/api/status")
async def status() -> dict[str, Any]:
    s = get_settings()
    return {"calls": manager.snapshot(), "owner_name": s.owner_name, "live_model": s.live_model,
            "postcall_model": s.postcall_model, "vobiz_enabled": s.vobiz_enabled, "vobiz_number": s.vobiz_number,
            "local_audio": local_audio_available(), "auth": auth.auth_required(), "last_error": manager.last_error}


@app.post("/api/call/test")
async def start_test_call(body: TestCall) -> dict[str, Any]:
    """Talk to the assistant through this computer's mic + headphones (free, local only)."""
    if manager.active_count() >= get_settings().max_concurrent_calls:
        raise HTTPException(409, "Too many calls running")
    from .transports.local import LocalAudioTransport
    try:
        transport = LocalAudioTransport(get_settings())
    except AudioDeviceError as e:
        raise HTTPException(400, str(e)) from None
    manager.start_background(CallOptions(mode="test", direction=body.direction, contact_name=body.contact_name.strip(),
                                         purpose=body.purpose.strip()), transport)
    return {"ok": True}


@app.post("/api/call/outbound")
async def start_outbound_call(body: OutboundCall) -> dict[str, Any]:
    s = get_settings()
    if not s.vobiz_enabled:
        raise HTTPException(400, "Phone calling isn't configured: set the VOBIZ_* and PUBLIC_BASE_URL values in .env.")
    to = to_e164(body.to)
    if not to:
        raise HTTPException(422, "That doesn't look like a valid phone number")
    if manager.active_count() >= s.max_concurrent_calls:
        raise HTTPException(409, "Too many calls running")
    name = body.contact_name.strip() or await repo.contact_name_for(to)
    token = secrets.token_urlsafe(16)
    manager._gc()
    manager.outbound[token] = Pending(CallOptions(mode="phone", direction="outgoing", contact_name=name,
                                                  contact_phone=to, purpose=body.purpose.strip()))
    try:
        call_uuid = await make_call(to, f"{vobiz_url('answer')}?out={token}", vobiz_url("hangup"))
    except VobizError as e:
        manager.outbound.pop(token, None)
        raise HTTPException(502, str(e)) from None
    bus.publish({"type": "dialing", "to": to, "contact_name": name})
    return {"ok": True, "call_uuid": call_uuid}


@app.post("/api/calls/live/{key}/stop")
async def stop_call(key: str) -> dict[str, Any]:
    live = manager.find(key)
    if not live:
        raise HTTPException(404, "That call is no longer active")
    live.agent.request_stop("user")
    return {"ok": True}


@app.get("/api/calls")
async def calls(q: str = Query("", max_length=200), limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> Any:
    return jsonable_encoder(await repo.list_calls(q, limit, offset))


@app.get("/api/calls/{call_id}")
async def call_detail(call_id: int) -> Any:
    c = await repo.get_call(call_id)
    if not c:
        raise HTTPException(404, "Call not found")
    c["has_audio"] = bool(c.get("recording_path") and Path(c["recording_path"]).exists())
    c.pop("recording_path", None)  # never expose filesystem paths to the browser
    return jsonable_encoder(c)


@app.get("/api/calls/{call_id}/audio")
async def call_audio(call_id: int) -> FileResponse:
    c = await repo.get_call(call_id)
    if not c or not c.get("recording_path"):
        raise HTTPException(404, "No recording for this call")
    p = Path(c["recording_path"]).resolve()
    if get_settings().recordings_dir not in p.parents or not p.exists():
        raise HTTPException(404, "Recording file missing")
    return FileResponse(p, media_type="audio/wav", filename=p.name)


@app.delete("/api/calls/{call_id}")
async def delete_call(call_id: int) -> dict[str, Any]:
    if any(c.agent.call_id == call_id for c in manager.calls.values()):
        raise HTTPException(409, "This call is live")
    path = await repo.delete_call(call_id)
    if path:
        p = Path(path).resolve()
        if get_settings().recordings_dir in p.parents:
            p.unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/calls/{call_id}/reprocess")
async def reprocess(call_id: int) -> dict[str, Any]:
    if not await repo.get_call(call_id):
        raise HTTPException(404, "Call not found")
    await repo.requeue_call(call_id)
    return {"ok": True}


@app.get("/api/contacts")
async def contacts() -> Any:
    return jsonable_encoder(await repo.list_contacts())


@app.put("/api/contacts")
async def put_contact(body: ContactIn) -> dict[str, Any]:
    phone = to_e164(body.phone)
    if not phone:
        raise HTTPException(422, "Invalid phone number")
    await repo.upsert_contact(phone, body.name.strip())
    return {"ok": True, "phone": phone}


@app.delete("/api/contacts/{phone}")
async def delete_contact(phone: str) -> dict[str, Any]:
    if not await repo.delete_contact(to_e164(phone)):
        raise HTTPException(404, "Contact not found")
    return {"ok": True}


@app.get("/api/memory")
async def memory() -> Any:
    return jsonable_encoder(await repo.list_facts())


@app.post("/api/memory")
async def add_memory(body: FactIn) -> Any:
    row = await repo.add_fact(body.fact.strip(), body.contact_name.strip())
    if not row:
        raise HTTPException(409, "That fact already exists")
    return jsonable_encoder(row)


@app.delete("/api/memory/{fact_id}")
async def delete_memory(fact_id: int) -> dict[str, Any]:
    if not await repo.delete_fact(fact_id):
        raise HTTPException(404, "Fact not found")
    return {"ok": True}


@app.get("/api/settings")
async def get_app_settings() -> dict[str, Any]:
    return {"values": await repo.get_app_settings(), "voices": GEMINI_VOICES}


@app.put("/api/settings")
async def put_app_settings(body: SettingsIn) -> dict[str, Any]:
    await repo.update_app_settings({k: v for k, v in body.model_dump().items() if v is not None})
    return {"values": await repo.get_app_settings()}


@app.get("/api/actions")
async def actions() -> Any:
    return jsonable_encoder(await repo.list_open_actions())


@app.patch("/api/actions/{item_id}")
async def patch_action(item_id: int, body: ActionPatch) -> dict[str, Any]:
    if not await repo.set_action_done(item_id, body.done):
        raise HTTPException(404, "Action item not found")
    return {"ok": True}


@app.get("/api/analytics")
async def analytics() -> Any:
    return jsonable_encoder(await repo.analytics(get_settings().timezone))


@app.get("/api/telephony")
async def telephony() -> dict[str, Any]:
    """What to paste into the Vobiz console, and current readiness."""
    s = get_settings()
    missing = [k for k, v in {"VOBIZ_AUTH_ID": s.vobiz_auth_id, "VOBIZ_AUTH_TOKEN": s.vobiz_auth_token,
                              "VOBIZ_NUMBER": s.vobiz_number, "VOBIZ_WEBHOOK_SECRET": s.vobiz_webhook_secret,
                              "PUBLIC_BASE_URL": s.public_base_url, "OWNER_PHONE": s.owner_phone}.items() if not v]
    return {"enabled": s.vobiz_enabled, "missing": missing, "number": s.vobiz_number, "owner_phone": s.owner_phone,
            "answer_url": vobiz_url("answer") if s.public_base_url and s.vobiz_webhook_secret else "",
            "hangup_url": vobiz_url("hangup") if s.public_base_url and s.vobiz_webhook_secret else ""}


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    origin = websocket.headers.get("origin", "")
    origin_host = origin.split("://", 1)[-1].split(":", 1)[0].lower() if origin else ""
    if (origin and origin_host not in _allowed_hosts()) or not auth.is_authenticated(websocket.cookies):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    q = bus.subscribe()
    try:
        await websocket.send_json({"type": "snapshot", "calls": jsonable_encoder(manager.snapshot())})
        while True:
            ev = await q.get()
            await websocket.send_json(jsonable_encoder(ev))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        bus.unsubscribe(q)


def main() -> None:
    import uvicorn
    s = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.use_selector_loop_on_windows()
    config = uvicorn.Config(app, host=s.host, port=s.port, log_level="info", ws_ping_interval=20, loop="none",
                            proxy_headers=True, forwarded_allow_ips="127.0.0.1")
    server = uvicorn.Server(config)
    print(f"\n  Dashboard: {s.public_base_url or f'http://{s.host}:{s.port}'}\n")
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
