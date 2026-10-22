"""Full CallAgent run over the Vobiz transport with a scripted fake Gemini and in-memory DB."""
import asyncio
import base64
import os
import wave

import numpy as np

os.environ.setdefault("GEMINI_API_KEY", "dummy_key_1234")
os.environ.setdefault("DATABASE_URL", "postgresql://x:y@localhost/z")

from tests.test_cloud import FakeVobizSocket, media  # noqa: E402


class FakeBrain:
    """Plays the role of Gemini Live: greets, gets interrupted, answers, says bye, hangs up."""
    instances: list["FakeBrain"] = []

    def __init__(self, *, handlers, **kw):
        self.h = handlers
        self.reconnects = 0
        self.prompt = kw["system_prompt"]
        FakeBrain.instances.append(self)

    async def run(self, audio_in, stop):
        h = self.h
        tone = (np.sin(np.arange(2400) / 24000 * 2 * np.pi * 300) * 8000).astype("<i2").tobytes()  # 100 ms @24k
        h.on_status("live", "Connected")
        h.on_output_text("Hi, this is Yogesh's AI assistant, and this call is recorded.")
        for _ in range(20):
            h.on_audio(tone)  # 2 s of greeting audio
        await asyncio.sleep(0.3)
        h.on_input_text("Wait, ", False)
        h.on_interrupted()  # caller barged in
        h.on_input_text("is he free tomorrow?", True)
        got = 0
        while got < 5:  # caller audio really reaches the model
            await asyncio.wait_for(audio_in.get(), 2)
            got += 1
        h.on_output_text("He'll confirm and call you back. Bye!")
        for _ in range(3):
            h.on_audio(tone)
        h.on_turn_complete()
        h.on_end_call("conversation finished")
        await stop.wait()


def test_full_call(monkeypatch, tmp_path):
    from app import agent as agent_mod, config, repo
    config.get_settings.cache_clear()
    monkeypatch.setenv("RECORDINGS_DIR", str(tmp_path))
    config.get_settings.cache_clear()
    db: dict = {"segments": [], "finished": None}

    async def get_app_settings():
        return {"style_rules": "Be chill", "voice": "Puck", "recording_enabled": True, "silence_timeout_s": 30}

    async def none(*a, **k):
        return []

    async def create_call(**k):
        db["created"] = k
        return 42

    async def set_recording_path(cid, p):
        db["rec"] = p

    async def insert_segment(cid, seq, speaker, text, s, e, interrupted):
        db["segments"].append((speaker, text, interrupted))

    async def finish_call(cid, **k):
        db["finished"] = k

    for name, fn in dict(get_app_settings=get_app_settings, list_facts=none, recent_summaries=none, create_call=create_call,
                         set_recording_path=set_recording_path, insert_segment=insert_segment, finish_call=finish_call).items():
        monkeypatch.setattr(repo, name, fn)
    monkeypatch.setattr(agent_mod, "GeminiLiveBrain", FakeBrain)

    from app.agent import CallAgent, CallOptions
    from app.transports.vobiz import VobizTransport

    async def scenario():
        ws = FakeVobizSocket()
        t = VobizTransport(ws, {"streamId": "s1", "callId": "c1", "mediaFormat": {"encoding": "audio/x-l16", "sampleRate": 16000}})
        agent = CallAgent(CallOptions(mode="phone", direction="incoming", contact_phone="+919876543210", provider_call_id="c1"), t)

        async def caller():  # Vobiz keeps streaming caller audio every 20 ms
            voice = np.full(320, 3000, np.int16)
            while not t.closed.is_set():
                await ws.inbox.put(media(voice))
                await asyncio.sleep(0.02)
        feeder = asyncio.create_task(caller())
        call_id = await asyncio.wait_for(agent.run(), 15)
        feeder.cancel()
        return call_id, agent, ws

    call_id, agent, ws = asyncio.run(scenario())
    assert call_id == 42
    fin = db["finished"]
    assert fin["end_reason"] == "assistant_hung_up" and fin["error"] == ""
    # transcript persisted, ordered, with the interruption flagged
    speakers = [s[0] for s in db["segments"]]
    assert speakers.count("assistant") == 2 and "caller" in speakers
    assert any(s[0] == "assistant" and s[2] for s in db["segments"])  # greeting was interrupted
    assert "Caller: Wait, is he free tomorrow?" in fin["transcript_text"]
    # barge-in cleared Vobiz's queue; the goodbye was played; then the call was hung up
    events = [m["event"] for m in ws.sent]
    assert "clearAudio" in events and events[-1] == "stop"
    last_clear = max(i for i, e in enumerate(events) if e == "clearAudio")
    assert events[last_clear + 1:].count("playAudio") >= 10  # the 300 ms goodbye (15 frames) got through
    # honesty + hang-up rules are in the live prompt
    p = FakeBrain.instances[-1].prompt
    assert "AI assistant" in p and "end_call" in p and "+919876543210" in p
    # stereo recording exists and both channels have audio
    with wave.open(fin["recording_path"]) as w:
        assert w.getnchannels() == 2 and w.getframerate() == 16000
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").reshape(-1, 2)
    assert np.abs(data[:, 0]).max() > 1000 and np.abs(data[:, 1]).max() > 1000
    config.get_settings.cache_clear()
