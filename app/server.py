"""FastAPI backend + dashboard. Bound to 127.0.0.1 only.

Run: python -m app.server   (or `python run.py` to start server + worker together)
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import db, repo
from .agent import CallAgent, CallOptions
from .audio.devices import AudioDeviceError, list_devices
from .audio.recorder import finalize_orphan
from .config import GEMINI_VOICES, get_settings
from .events import bus
from .migrate import migrate

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


# ---------------------------------------------------------------- live call manager
class AgentManager:
    def __init__(self) -> None:
        self.agent: CallAgent | None = None
        self.task: asyncio.Task[int | None] | None = None
        self.last_error = ""

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    async def start(self, opts: CallOptions) -> None:
        if self.busy:
            raise HTTPException(409, "A call session is already running. Stop it first.")
        self.last_error = ""
        self.agent = CallAgent(opts)
        self.task = asyncio.create_task(self._run(self.agent), name="call")

    async def _run(self, agent: CallAgent) -> int | None:
        try:
            return await agent.run()
        except AudioDeviceError as e:
            self.last_error = str(e)
        except Exception as e:  # never let a call crash the server
            log.exception("call task crashed")
            self.last_error = f"Call failed: {e}"
            bus.publish({"type": "status", "state": "error", "message": self.last_error})
        return None

    def stop(self) -> bool:
        if self.busy and self.agent:
            self.agent.request_stop("user")
            return True
        return False

    def snapshot(self) -> dict[str, Any]:
        if self.busy and self.agent:
            return {"active": True, **self.agent.snapshot()}
        state = "error" if self.last_error or (self.agent and self.agent.error) else "idle"
        msg = self.last_error or (self.agent.error if self.agent else "")
        return {"active": False, "state": state, "message": msg,
                "last_call_id": self.agent.call_id if self.agent else None}


manager = AgentManager()


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
    yield
    manager.stop()
    if manager.task:
        try:
            await asyncio.wait_for(manager.task, timeout=20)
        except Exception:
            pass
    await db.close_pool()


app = FastAPI(title="Personal Call Assistant", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def local_only(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Blocks DNS-rebinding: only accept requests addressed to localhost."""
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
    if host not in ("127.0.0.1", "localhost", "::1"):
        return JSONResponse({"detail": "forbidden host"}, status_code=403)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; "
        "img-src 'self' data:; media-src 'self'; connect-src 'self' ws://127.0.0.1:* ws://localhost:*; frame-ancestors 'none'"
    )
    return resp


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error")
    msg = db.describe_db_error(exc) if "psycopg" in type(exc).__module__ else "Internal error, see server log."
    return JSONResponse({"detail": msg}, status_code=500)


# ---------------------------------------------------------------- schemas
Str200 = Field("", max_length=200)


class StartCall(BaseModel):
    mode: Literal["phone", "test"] = "phone"
    direction: Literal["incoming", "outgoing"] = "incoming"
    contact_name: str = Str200
    contact_phone: str = Field("", max_length=32, pattern=r"^[0-9+\-() ]*$")
    purpose: str = Field("", max_length=1000)

    @field_validator("contact_name", "purpose")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


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
    contact_name: str = Str200


class ActionPatch(BaseModel):
    done: bool


# ---------------------------------------------------------------- routes
@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/api/status")
async def status() -> dict[str, Any]:
    s = get_settings()
    return {"call": manager.snapshot(), "owner_name": s.owner_name, "live_model": s.live_model,
            "postcall_model": s.postcall_model}


@app.post("/api/call/start")
async def start_call(body: StartCall) -> dict[str, Any]:
    await manager.start(CallOptions(**body.model_dump()))
    return {"ok": True}


@app.post("/api/call/stop")
async def stop_call() -> dict[str, Any]:
    return {"ok": manager.stop()}


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
    if manager.busy and manager.agent and manager.agent.call_id == call_id:
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


@app.get("/api/devices")
async def devices() -> dict[str, Any]:
    s = get_settings()
    from .audio.devices import find_device
    resolved: dict[str, str] = {}
    for label, kind, sub in [("caller_input", "input", s.caller_input_device), ("ai_output", "output", s.ai_output_device),
                             ("test_input", "input", s.test_input_device), ("test_output", "output", s.test_output_device)]:
        try:
            d = find_device(kind, sub, s.audio_hostapi)
            resolved[label] = f"{d.name} ({d.hostapi})"
        except AudioDeviceError as e:
            resolved[label] = f"NOT FOUND: {e}"
    return {"resolved": resolved,
            "inputs": [d.name for d in list_devices("input") if d.hostapi == s.audio_hostapi],
            "outputs": [d.name for d in list_devices("output") if d.hostapi == s.audio_hostapi]}


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    origin = websocket.headers.get("origin", "")
    if origin and not any(origin.startswith(p) for p in ("http://127.0.0.1", "http://localhost")):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    q = bus.subscribe()
    try:
        await websocket.send_json({"type": "snapshot", **jsonable_encoder(manager.snapshot())})
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
    config = uvicorn.Config(app, host=s.host, port=s.port, log_level="info", ws_ping_interval=20, loop="none")
    server = uvicorn.Server(config)
    print(f"\n  Dashboard: http://{s.host}:{s.port}\n")
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
