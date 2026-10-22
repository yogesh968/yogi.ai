"""Vobiz bidirectional media stream transport (WebSocket).

Protocol (verified against Vobiz docs + their official Gemini Live bridge):
  Vobiz -> us : {"event":"start","start":{"callId","streamId","mediaFormat":{...}}}, then
                {"event":"media","media":{"payload": base64 L16 16 kHz LE}} every 20 ms,
                {"event":"clearedAudio"}, {"event":"playedStream"}. Socket close = call over.
  us -> Vobiz : {"event":"playAudio","streamId","media":{"contentType":"audio/x-l16","sampleRate":24000,"payload"}},
                {"event":"clearAudio","streamId"}, {"event":"stop","streamId"} (runs next XML verb: <Hangup/>).

We pace AI audio out in real time with a small lead instead of dumping whole replies
into Vobiz's queue. That keeps barge-in instant (our buffer is the queue) and gives
an exact timeline for the stereo recording.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any

import numpy as np
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ..audio.playback import PlaybackBuffer
from ..audio.resample import StreamResampler
from .base import CALLER_RATE, AudioSink, AudioTransport

log = logging.getLogger(__name__)
OUT_RATE = 24000          # Gemini's native output rate; Vobiz accepts L16/24000 on playAudio
FRAME_MS = 20
LEAD_S = 0.06             # how far ahead of real time we keep Vobiz's playback queue
CLEAR_GATE_S = 0.3        # max wait for clearedAudio ack before sending new audio


def _mulaw_table() -> np.ndarray:
    b = ~np.arange(256, dtype=np.int32) & 0xFF
    t = ((b & 0x0F) << 3) + 0x84
    t <<= (b & 0x70) >> 4
    return np.where(b & 0x80, 0x84 - t, t - 0x84).astype(np.int16)


_MULAW = _mulaw_table()


class VobizTransport(AudioTransport):
    can_hang_up = True

    def __init__(self, ws: WebSocket, start: dict[str, Any]) -> None:
        super().__init__()
        self.ws = ws
        self.stream_id: str = start.get("streamId", "")
        self.call_id: str = start.get("callId", "")
        fmt = start.get("mediaFormat") or {}
        self.in_encoding = str(fmt.get("encoding", "audio/x-l16")).lower()
        self.in_rate = int(fmt.get("sampleRate", 16000))
        self.output_rate = OUT_RATE
        self.playback = PlaybackBuffer(OUT_RATE)
        self._send_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[None]] = []
        self._sent_until = 0.0
        self._clear_gate_until = 0.0
        self._stopping = False
        self._resample_in = None if self.in_rate == CALLER_RATE else StreamResampler(self.in_rate, CALLER_RATE, quality="LQ")

    def describe(self) -> dict[str, str]:
        return {"input": f"Vobiz stream ({self.in_encoding} {self.in_rate} Hz)", "output": f"Vobiz playAudio (L16 {OUT_RATE} Hz)"}

    async def start(self, sink: AudioSink) -> None:
        self._tasks = [asyncio.create_task(self._reader(sink), name="vobiz-reader"),
                       asyncio.create_task(self._pacer(sink), name="vobiz-pacer")]

    async def _send(self, msg: dict[str, Any]) -> bool:
        if self.ws.client_state != WebSocketState.CONNECTED:
            return False
        async with self._send_lock:
            try:
                await self.ws.send_text(json.dumps(msg))
                return True
            except Exception:
                self.closed.set()
                return False

    def _decode(self, payload: str) -> np.ndarray:
        raw = base64.b64decode(payload)
        if "mulaw" in self.in_encoding or "pcmu" in self.in_encoding:
            pcm = _MULAW[np.frombuffer(raw, dtype=np.uint8)]
        else:
            pcm = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2")
        return self._resample_in.process(pcm) if self._resample_in else pcm

    async def _reader(self, sink: AudioSink) -> None:
        try:
            while True:
                data = json.loads(await self.ws.receive_text())
                ev = data.get("event")
                if ev == "media":
                    media = data.get("media") or {}
                    if media.get("track", "inbound") == "inbound" and media.get("payload"):
                        pcm = self._decode(media["payload"])
                        if pcm.size:
                            sink.caller_audio(pcm, time.monotonic())
                elif ev == "clearedAudio":
                    self._clear_gate_until = 0.0
                elif ev == "start":  # Vobiz reconnect replay (maxRetries): adopt the new stream id
                    self.stream_id = (data.get("start") or {}).get("streamId", self.stream_id)
                elif ev == "stop":
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception:
            log.exception("vobiz reader error")
        finally:
            self.closed.set()

    async def _pacer(self, sink: AudioSink) -> None:
        frame = OUT_RATE * FRAME_MS // 1000
        dur = frame / OUT_RATE
        zeros = np.zeros(frame, dtype=np.int16)
        next_tick = time.monotonic()
        sink.ai_played(zeros, 0, next_tick)  # anchor the AI recording track at stream start
        while not self.closed.is_set():
            now = time.monotonic()
            if self._sent_until < now:
                self._sent_until = now
            sent = False
            if now >= self._clear_gate_until:
                while self._sent_until - now < LEAD_S:
                    pcm, real = self.playback.read(frame)
                    if real == 0:
                        break
                    ok = await self._send({"event": "playAudio", "streamId": self.stream_id, "media": {
                        "contentType": "audio/x-l16", "sampleRate": OUT_RATE,
                        "payload": base64.b64encode(pcm.astype("<i2").tobytes()).decode()}})
                    if not ok:
                        return
                    self._sent_until += dur
                    sink.ai_played(pcm, real, self._sent_until)
                    sent = True
            if not sent and self._sent_until <= now + 1e-3:
                sink.ai_played(zeros, 0, now)  # keep the AI track continuous during silence
            next_tick += dur
            delay = next_tick - time.monotonic()
            if delay < -0.2:  # event loop stalled: resync instead of bursting
                next_tick = time.monotonic()
                delay = 0
            await asyncio.sleep(max(0.0, delay))

    def interrupt(self) -> int:
        dropped = self.playback.clear()
        self._sent_until = time.monotonic()
        self._clear_gate_until = self._sent_until + CLEAR_GATE_S
        asyncio.get_running_loop().create_task(self._send({"event": "clearAudio", "streamId": self.stream_id}))
        return dropped

    async def hang_up(self) -> None:
        """`stop` ends the stream; Vobiz then runs the next XML verb, which is <Hangup/>."""
        if not self.closed.is_set() and not self._stopping:
            self._stopping = True
            await self._send({"event": "stop", "streamId": self.stream_id})

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self.ws.client_state == WebSocketState.CONNECTED:
            try:
                await self.ws.close()
            except Exception:
                pass
