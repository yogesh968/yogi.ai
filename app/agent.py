"""CallAgent: runs one call end-to-end over any AudioTransport.

  transport --caller 16 kHz--> [queue, drop-oldest] --> Gemini Live
  Gemini 24 kHz --> renderer (resample to transport rate) --> transport.playback --> caller
Both directions also feed the stereo recorder (L caller / R AI, 16 kHz).

Nothing slow runs on the audio path: DB writes go through a background queue and
dashboard events are fire-and-forget.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np

from . import repo
from .audio.recorder import StereoRecorder
from .audio.resample import StreamResampler, rms
from .config import Settings, get_settings
from .events import bus
from .prompts import CallContext, build_system_prompt
from .transcript import Segment, TranscriptAssembler
from .transports.base import AudioTransport
from .voice.base import NativeAudioRenderer, SpeechRenderer
from .voice.gemini_live import GeminiLiveBrain, LiveFatalError, LiveHandlers

log = logging.getLogger(__name__)
ONSET_GAP_S = 0.6          # silence needed before a new caller "onset" is registered
MAX_INPUT_BACKLOG_S = 0.5  # never send Gemini audio older than this
HANGUP_DRAIN_MAX_S = 10.0  # max wait for the goodbye to finish playing before hanging up
ATTACH_TIMEOUT_S = 20.0    # early start: max wait for the phone's media stream after the call is answered


@dataclass
class CallOptions:
    mode: Literal["phone", "test"] = "test"
    direction: Literal["incoming", "outgoing"] = "incoming"
    contact_name: str = ""
    contact_phone: str = ""
    purpose: str = ""
    provider_call_id: str = ""


class CallAgent:
    def __init__(self, opts: CallOptions, transport: AudioTransport | None, settings: Settings | None = None,
                 *, can_hang_up: bool = True) -> None:
        """`transport=None` = early start: connect Gemini and pre-generate the greeting while the phone
        line's media stream is still being set up; `attach()` the transport when it arrives."""
        self.opts = opts
        self.transport = transport
        self.can_hang_up = transport.can_hang_up if transport else can_hang_up
        self._attached = asyncio.Event()
        if transport:
            self._attached.set()
        self._renderer: SpeechRenderer | None = None
        self._preroll: list[bytes] = []  # model audio produced before the transport was attached
        self.s = settings or get_settings()
        self.call_id: int | None = None
        self.key = ""  # set by the call manager; tags every dashboard event
        self.state = "starting"
        self.started_at = time.monotonic()
        self.end_reason = ""
        self.error = ""
        self._stop = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._audio_in: asyncio.Queue[bytes] = asyncio.Queue(maxsize=max(2, int(MAX_INPUT_BACKLOG_S * 1000 / 20)))
        self._db_q: asyncio.Queue[Segment | None] = asyncio.Queue()
        self.recorder: StereoRecorder | None = None
        self._ai_rec_res: StreamResampler | None = None
        self.assembler = TranscriptAssembler(on_update=self._on_segment)
        # activity tracking (written from audio threads; float writes are atomic under the GIL)
        self._last_caller_voice = 0.0
        self._last_ai_audio = 0.0
        self._last_event = time.monotonic()
        self._caller_onset_ms: int | None = None
        self._caller_level = 0.0
        self._ai_level = 0.0
        self._ai_playing = False
        self.last_latency_ms: int | None = None
        self.interruptions = 0

    # ------------------------------------------------------------ public
    def attach(self, transport: AudioTransport) -> None:
        self.transport = transport
        self._attached.set()

    def request_stop(self, reason: str = "user") -> None:
        if not self.end_reason:
            self.end_reason = reason
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        else:
            self._stop.set()

    @property
    def active(self) -> bool:
        return self.state in ("starting", "connecting", "live", "reconnecting", "ending")

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state, "call_id": self.call_id, "mode": self.opts.mode, "direction": self.opts.direction,
            "contact_name": self.opts.contact_name, "contact_phone": self.opts.contact_phone, "purpose": self.opts.purpose,
            "elapsed_s": round(time.monotonic() - self.started_at), "latency_ms": self.last_latency_ms,
            "segments": [s.as_dict() for s in self.assembler.ordered(final_only=False)],
        }

    async def run(self) -> int | None:
        self._loop = asyncio.get_running_loop()
        try:
            return await self._run()
        finally:
            if self.error:
                self._status("error", self.error)
            else:
                self._status("idle", f"Call ended ({self.end_reason or 'done'})")

    # ------------------------------------------------------------ AudioSink (called from audio threads / pacer)
    def caller_audio(self, pcm: np.ndarray, t: float) -> None:
        level = rms(pcm)
        self._caller_level = level
        if level > self.s.silence_rms_threshold:
            if t - self._last_caller_voice > ONSET_GAP_S:
                self._caller_onset_ms = int((t - self.started_at) * 1000)
            self._last_caller_voice = t
        if self.recorder:
            self.recorder.write_caller(pcm, t)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._enqueue, pcm.tobytes())

    def ai_played(self, pcm: np.ndarray, real: int, t: float) -> None:
        if real:
            if not self._ai_playing:
                self._ai_playing = True
                gap = t - self._last_caller_voice  # caller stopped talking -> first AI sound out
                if 0 < gap < 5:
                    self.last_latency_ms = int(gap * 1000)
                    self._pub_threadsafe({"type": "latency", "call_id": self.call_id, "ms": self.last_latency_ms})
            self._last_ai_audio = t
            self._ai_level = rms(pcm[:real])
        else:
            self._ai_playing = False
            self._ai_level = 0.0
        if self.recorder and self._ai_rec_res:
            self.recorder.write_ai(self._ai_rec_res.process(pcm), t)

    def _enqueue(self, b: bytes) -> None:
        q = self._audio_in
        if q.full():  # drop oldest: low latency beats completeness
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(b)

    # ------------------------------------------------------------ internals
    def _pub(self, ev: dict[str, Any]) -> None:
        bus.publish({**ev, "key": self.key})

    def _pub_threadsafe(self, ev: dict[str, Any]) -> None:
        bus.publish_threadsafe({**ev, "key": self.key})

    def _now_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)

    def _status(self, state: str, message: str = "") -> None:
        self.state = state
        log.info("[call %s] %s %s", self.call_id, state, message)
        self._pub({"type": "status", "state": state, "message": message, "call_id": self.call_id,
                     "mode": self.opts.mode, "direction": self.opts.direction,
                     "contact_name": self.opts.contact_name, "contact_phone": self.opts.contact_phone})

    async def _run(self) -> int | None:
        s = self.s
        self._status("connecting", "Preparing call…")
        app, facts, recent = await asyncio.gather(  # one round trip instead of two: the caller is waiting
            repo.get_app_settings(),
            repo.list_facts(self.opts.contact_name, self.opts.contact_phone),
            repo.recent_summaries(self.opts.contact_name, self.opts.contact_phone),
        )
        recording = bool(app["recording_enabled"])
        voice = str(app["voice"])
        silence_timeout = int(app["silence_timeout_s"])
        prompt = build_system_prompt(
            owner_name=s.owner_name, languages=s.caller_languages, timezone=s.timezone,
            style_rules=str(app["style_rules"]), facts=facts, recent=recent,
            ctx=CallContext(self.opts.direction, self.opts.contact_name, self.opts.purpose, recording,
                            contact_phone=self.opts.contact_phone, can_hang_up=self.can_hang_up),
        )
        self.call_id = await repo.create_call(
            direction=self.opts.direction, mode=self.opts.mode, contact_name=self.opts.contact_name,
            contact_phone=self.opts.contact_phone, purpose=self.opts.purpose, voice=voice,
            provider_call_id=self.opts.provider_call_id,
        )
        self._status("connecting", "Connecting to Gemini Live…")
        rec_path: Path | None = None
        db_task = asyncio.create_task(self._db_writer(), name="db-writer")
        brain: GeminiLiveBrain | None = None
        tasks: set[asyncio.Task[Any]] = set()
        t: AudioTransport | None = None
        try:
            def on_audio(pcm: bytes) -> None:
                if self._renderer is None:
                    self._preroll.append(pcm)  # greeting generated before the phone audio is connected
                else:
                    self._renderer.on_model_audio(pcm)

            def on_interrupted() -> None:
                self._preroll.clear()
                dropped = 0
                if self.transport is not None and self._renderer is not None:
                    dropped = round(self.transport.interrupt() * 1000 / self.transport.output_rate)
                    self._renderer.interrupt()
                self.interruptions += 1
                self.assembler.interrupted(self._now_ms())
                self._last_event = time.monotonic()
                self._pub({"type": "interrupted", "call_id": self.call_id, "dropped_ms": dropped})

            def on_input_text(text: str, finished: bool) -> None:
                self._last_event = time.monotonic()
                self.assembler.caller_text(text, self._now_ms(), self._caller_onset_ms, finished)

            def on_output_text(text: str) -> None:
                self._last_event = time.monotonic()
                if self._renderer is not None:
                    self._renderer.on_model_text(text)
                self.assembler.assistant_text(text, self._now_ms())

            def on_end_call(reason: str) -> None:
                log.info("[call %s] assistant ended the call: %s", self.call_id, reason)
                asyncio.get_running_loop().create_task(self._finish_after_playback("assistant_hung_up"))

            brain = GeminiLiveBrain(
                api_key=s.gemini_api_key, model=s.live_model, voice=voice, system_prompt=prompt,
                handlers=LiveHandlers(
                    on_audio=on_audio, on_input_text=on_input_text, on_output_text=on_output_text,
                    on_interrupted=on_interrupted, on_turn_complete=lambda: self.assembler.turn_complete(self._now_ms()),
                    on_status=lambda st, m: self._status(st, m), transcript_so_far=self.assembler.as_text,
                    on_end_call=on_end_call if self.can_hang_up else None,
                ),
                vad_start=s.vad_start_sensitivity, vad_end=s.vad_end_sensitivity, vad_silence_ms=s.vad_silence_ms,
            )
            # Gemini connects (and starts generating the greeting) while we wait for the phone audio.
            brain_task = asyncio.create_task(brain.run(self._audio_in, self._stop), name="live-brain")
            stop_task = asyncio.create_task(self._stop.wait(), name="stop")
            tasks = {brain_task, stop_task}
            if not self._attached.is_set():
                attach_task = asyncio.create_task(self._attached.wait(), name="attach")
                await asyncio.wait({attach_task, brain_task, stop_task}, timeout=ATTACH_TIMEOUT_S,
                                   return_when=asyncio.FIRST_COMPLETED)
                attach_task.cancel()
                if not self._attached.is_set():
                    if not brain_task.done() and not self._stop.is_set():
                        self.end_reason = self.end_reason or "no_audio_stream"
                    self._stop.set()
            if self._attached.is_set() and not self._stop.is_set() and not brain_task.done():
                t = self.transport
                assert t is not None
                if recording:
                    rec_path = s.recordings_dir / f"call_{self.call_id}_{datetime.now():%Y%m%d_%H%M%S}.wav"
                    self._ai_rec_res = StreamResampler(t.output_rate, s.record_sample_rate, quality="LQ")
                    self.recorder = StereoRecorder(rec_path, s.record_sample_rate)
                    self.recorder.start()
                renderer: SpeechRenderer = NativeAudioRenderer(t.output_rate, t.playback.push)
                for pcm in self._preroll:  # play the already-generated greeting immediately
                    renderer.on_model_audio(pcm)
                self._preroll.clear()
                self._renderer = renderer
                await t.start(self)
                if rec_path:
                    await repo.set_recording_path(self.call_id, str(rec_path))
                self._pub({"type": "devices", "call_id": self.call_id, **t.describe()})
                tasks |= {
                    asyncio.create_task(self._watchdog(silence_timeout), name="watchdog"),
                    asyncio.create_task(t.closed.wait(), name="transport-closed"),
                }
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if t is not None and t.closed.is_set() and not self.end_reason:
                self.end_reason = "caller_hung_up"
            self._stop.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if brain_task.done() and not brain_task.cancelled() and brain_task.exception():
                raise brain_task.exception()  # type: ignore[misc]
        except LiveFatalError as e:
            self.error, self.end_reason = str(e), self.end_reason or "error"
        except asyncio.CancelledError:
            self.end_reason = self.end_reason or "cancelled"
            raise
        except Exception as e:
            log.exception("call failed")
            self.error, self.end_reason = f"Unexpected error: {e}", self.end_reason or "error"
        finally:
            # Always persist whatever we have, even after errors.
            self.state = "ending"
            self._pub({"type": "status", "state": "ending", "message": "Saving call…", "call_id": self.call_id})
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            t = self.transport
            if t is not None:
                try:
                    if self.end_reason in ("silence", "user", "error", "assistant_hung_up"):
                        await asyncio.wait_for(t.hang_up(), timeout=3)
                except Exception:
                    pass
                await t.stop()
            self.assembler.close_all(self._now_ms())
            saved = await asyncio.to_thread(self.recorder.finalize) if self.recorder else None
            await self._db_q.put(None)
            try:
                await asyncio.wait_for(db_task, timeout=30)
            except Exception:
                log.exception("transcript writer did not finish cleanly")
            await repo.finish_call(
                self.call_id, end_reason=self.end_reason or "user",
                recording_path=str(saved) if saved else (str(rec_path) if rec_path and rec_path.exists() else None),
                transcript_text=self.assembler.as_text(), error=self.error,
            )
            self._pub({"type": "call_saved", "call_id": self.call_id, "reconnects": brain.reconnects if brain else 0})
        return self.call_id

    async def _finish_after_playback(self, reason: str) -> None:
        """Let the goodbye finish playing, then end the call."""
        deadline = time.monotonic() + HANGUP_DRAIN_MAX_S
        await asyncio.sleep(0.4)  # the model may still be streaming the last words
        while time.monotonic() < deadline and self.transport is not None and (
                self.transport.playback.buffered_ms > 0 or self._ai_playing):
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.3)
        self.request_stop(reason)

    async def _watchdog(self, silence_timeout: int) -> None:
        """Ends the call after `silence_timeout` s with no speech from either side; streams level meters."""
        assert self.transport is not None
        pb = self.transport.playback
        while not self._stop.is_set():
            await asyncio.sleep(0.2)
            now = time.monotonic()
            self._pub({"type": "levels", "call_id": self.call_id,
                         "caller": round(min(1.0, self._caller_level / 4000), 3),
                         "ai": round(min(1.0, self._ai_level / 4000), 3), "buffer_ms": round(pb.buffered_ms)})
            last = max(self._last_caller_voice, self._last_ai_audio, self._last_event, self.started_at)
            if pb.buffered_ms < 1 and now - last > silence_timeout:
                self.end_reason = self.end_reason or "silence"
                log.info("silence timeout (%ss)", silence_timeout)
                return

    def _on_segment(self, seg: Segment) -> None:
        self._pub({"type": "segment", "call_id": self.call_id, **seg.as_dict()})
        if seg.final:
            self._db_q.put_nowait(seg)

    async def _db_writer(self) -> None:
        while True:
            seg = await self._db_q.get()
            if seg is None:
                return
            for attempt in range(3):
                try:
                    assert self.call_id is not None
                    await repo.insert_segment(self.call_id, seg.seq, seg.speaker, seg.text, seg.start_ms, seg.end_ms, seg.interrupted)
                    break
                except Exception as e:
                    log.warning("segment write failed (try %d): %s", attempt + 1, e)
                    await asyncio.sleep(0.5 * (attempt + 1))
