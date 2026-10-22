"""Cloud telephony pieces: phone numbers, auth, Vobiz media transport, webhooks + security middleware."""
import asyncio
import base64
import json
import time

import numpy as np
import pytest
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.phone import same_number, to_e164


# ---------------------------------------------------------------- phone numbers
@pytest.mark.parametrize("raw,expected", [
    ("+91 98765-43210", "+919876543210"), ("09876543210", "+919876543210"), ("919876543210", "+919876543210"),
    ("9876543210", "+919876543210"), ("0044 20 7946 0958", "+442079460958"), ("", ""), ("sip:a@b", ""), ("12", ""),
])
def test_to_e164(raw, expected):
    assert to_e164(raw) == expected


def test_same_number():
    assert same_number("98765 43210", "+91-9876543210")
    assert not same_number("", "")


# ---------------------------------------------------------------- Vobiz transport against a fake Vobiz socket
class FakeVobizSocket:
    def __init__(self) -> None:
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.client_state = WebSocketState.CONNECTED

    async def receive_text(self) -> str:
        msg = await self.inbox.get()
        if msg is None:
            self.client_state = WebSocketState.DISCONNECTED
            raise WebSocketDisconnect(1000)
        return json.dumps(msg)

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def close(self) -> None:
        self.client_state = WebSocketState.DISCONNECTED


class Sink:
    def __init__(self) -> None:
        self.caller: list[np.ndarray] = []
        self.ai: list[tuple[int, int, float]] = []

    def caller_audio(self, pcm, t):
        self.caller.append(pcm)

    def ai_played(self, pcm, real, t):
        self.ai.append((pcm.size, real, t))


def media(pcm: np.ndarray) -> dict:
    return {"event": "media", "media": {"track": "inbound", "payload": base64.b64encode(pcm.astype("<i2").tobytes()).decode()}}


def test_vobiz_transport_round_trip():
    from app.transports.vobiz import VobizTransport

    async def scenario():
        ws = FakeVobizSocket()
        t = VobizTransport(ws, {"streamId": "s1", "callId": "c1", "mediaFormat": {"encoding": "audio/x-l16", "sampleRate": 16000}})
        sink = Sink()
        await t.start(sink)
        # caller audio in: 3 x 20 ms frames of 16 kHz L16
        for _ in range(3):
            await ws.inbox.put(media(np.full(320, 1000, np.int16)))
        # AI audio out: 0.5 s at 24 kHz -> should be paced out in 960-byte (20 ms) frames
        t.playback.push(np.full(12000, 500, np.int16))
        t0 = time.monotonic()
        await asyncio.sleep(0.25)
        plays = [m for m in ws.sent if m["event"] == "playAudio"]
        assert len(sink.caller) == 3 and all(c.size == 320 and c[0] == 1000 for c in sink.caller)
        # paced: ~0.25 s elapsed + 60 ms lead => ~15 frames, definitely not all 25
        assert 8 <= len(plays) <= 20, len(plays)
        p = plays[0]
        assert p["streamId"] == "s1" and p["media"]["contentType"] == "audio/x-l16" and p["media"]["sampleRate"] == 24000
        assert len(base64.b64decode(p["media"]["payload"])) == 960
        # barge-in: buffer flushed locally and clearAudio sent
        dropped = t.interrupt()
        await asyncio.sleep(0.05)
        assert dropped > 0 and t.playback.buffered_ms == 0
        assert any(m["event"] == "clearAudio" and m["streamId"] == "s1" for m in ws.sent)
        n_after_clear = len([m for m in ws.sent if m["event"] == "playAudio"])
        await asyncio.sleep(0.1)
        assert len([m for m in ws.sent if m["event"] == "playAudio"]) == n_after_clear  # nothing more to play
        # AI recording track keeps ticking with silence
        assert sink.ai and sink.ai[-1][1] == 0
        # hang up -> stop event; remote close -> closed
        await t.hang_up()
        assert ws.sent[-1] == {"event": "stop", "streamId": "s1"}
        await ws.inbox.put(None)
        await asyncio.wait_for(t.closed.wait(), 1)
        await t.stop()
        assert time.monotonic() - t0 < 2

    asyncio.run(scenario())


def test_vobiz_transport_decodes_mulaw_8k():
    from app.transports.vobiz import VobizTransport

    async def scenario():
        ws = FakeVobizSocket()
        t = VobizTransport(ws, {"streamId": "s", "callId": "c", "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000}})
        sink = Sink()
        await t.start(sink)
        for _ in range(10):  # 200 ms of mu-law digital silence (0xFF)
            await ws.inbox.put({"event": "media", "media": {"payload": base64.b64encode(b"\xff" * 160).decode()}})
        await asyncio.sleep(0.1)
        total = sum(c.size for c in sink.caller)
        assert 2700 <= total <= 3300  # ~200 ms at 16 kHz; low-latency resampler holds back < 25 ms
        assert max(abs(int(c.max(initial=0))) for c in sink.caller) < 50
        await ws.inbox.put(None)
        await t.stop()

    asyncio.run(scenario())


# ---------------------------------------------------------------- server: webhooks, auth, security
@pytest.fixture()
def cloud_app(monkeypatch):
    env = {
        "GEMINI_API_KEY": "dummy_key_1234", "DATABASE_URL": "postgresql://x:y@localhost/z",
        "PUBLIC_BASE_URL": "https://me.duckdns.org", "DASHBOARD_PASSWORD": "correct horse battery",
        "SECRET_KEY": "s" * 40, "VOBIZ_AUTH_ID": "MA_TEST", "VOBIZ_AUTH_TOKEN": "tok", "VOBIZ_NUMBER": "+918000000000",
        "VOBIZ_WEBHOOK_SECRET": "w" * 24, "OWNER_PHONE": "+919999999999", "OWNER_NAME": "Yogesh",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from app import config, repo, server
    config.get_settings.cache_clear()

    async def fake_name(phone):
        return "Priya" if phone == "+919876543210" else ""
    monkeypatch.setattr(repo, "contact_name_for", fake_name)
    server.manager.pending.clear()
    from fastapi.testclient import TestClient
    client = TestClient(server.app, base_url="https://me.duckdns.org")
    yield client, server
    config.get_settings.cache_clear()


def test_answer_webhook_returns_stream_xml(cloud_app):
    client, server = cloud_app
    r = client.post(f"/vobiz/{'w' * 24}/answer", data={"CallUUID": "abc", "From": "919876543210", "To": "918000000000",
                                                       "ForwardedFrom": "919999999999", "Event": "StartApp"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    assert '<Stream bidirectional="true" audioTrack="inbound" keepCallAlive="true"' in r.text
    assert 'contentType="audio/x-l16;rate=16000"' in r.text
    assert f"wss://me.duckdns.org/vobiz/{'w' * 24}/ws" in r.text and "<Hangup/>" in r.text
    p = server.manager.pending["abc"].opts
    assert (p.contact_phone, p.contact_name, p.direction, p.mode) == ("+919876543210", "Priya", "incoming", "phone")


def test_answer_webhook_rejects_bad_secret_and_when_busy(cloud_app):
    client, server = cloud_app
    assert client.post("/vobiz/wrong-secret/answer", data={"CallUUID": "x"}).status_code == 404
    server.manager.active_count = lambda: 99  # type: ignore[method-assign]
    try:
        r = client.post(f"/vobiz/{'w' * 24}/answer", data={"CallUUID": "y", "From": "911234567890"})
        assert 'Hangup reason="busy"' in r.text
    finally:
        del server.manager.active_count


def test_outbound_token_maps_to_outgoing_context(cloud_app):
    client, server = cloud_app
    from app.agent import CallOptions
    server.manager.outbound["tok123"] = server.Pending(CallOptions(mode="phone", direction="outgoing", contact_phone="+911", purpose="Confirm"))
    client.post(f"/vobiz/{'w' * 24}/answer?out=tok123", data={"CallUUID": "out-1", "From": "918000000000"})
    p = server.manager.pending["out-1"].opts
    assert p.direction == "outgoing" and p.purpose == "Confirm" and p.provider_call_id == "out-1"


def test_dashboard_requires_login_and_csrf_header(cloud_app):
    client, _ = cloud_app
    assert client.get("/api/status").status_code == 401
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.post("/api/login", json={"password": "nope"}, headers={"X-Requested-With": "fetch"}).status_code == 401
    assert client.post("/api/login", json={"password": "correct horse battery"}).status_code == 403  # no CSRF header
    r = client.post("/api/login", json={"password": "correct horse battery"},
                    headers={"X-Requested-With": "fetch", "X-Forwarded-Proto": "https"})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "secure" in cookie
    assert client.get("/healthz").status_code == 200


def test_unknown_host_is_rejected(cloud_app):
    client, _ = cloud_app
    assert client.get("/healthz", headers={"host": "evil.example.com"}).status_code == 403


def test_login_rate_limit(cloud_app):
    client, _ = cloud_app
    from app import auth
    auth._failures.clear()
    codes = [client.post("/api/login", json={"password": "x"}, headers={"X-Requested-With": "fetch"}).status_code for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429
    auth._failures.clear()


def test_session_tokens():
    import os
    os.environ.setdefault("GEMINI_API_KEY", "dummy_key_1234")
    os.environ.setdefault("DATABASE_URL", "postgresql://x:y@localhost/z")
    from app import auth
    tok = auth.make_session()
    assert auth.valid_session(tok)
    exp, sig = tok.split(".")
    assert not auth.valid_session(f"{int(exp) + 1}.{sig}")  # tampered expiry
    assert not auth.valid_session("garbage") and not auth.valid_session(None)
