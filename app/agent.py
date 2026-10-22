"""CallAgent: runs one call end-to-end.

Audio path (phone mode):
  CABLE Output --(PortAudio in cb)--> resample->16k --> [queue, drop-oldest] --> Gemini Live
  Gemini 24k audio --> renderer (resample->device rate) --> PlaybackBuffer --(out cb)--> Hi-Fi Cable Input
Both callbacks also feed the stereo recorder (L caller / R AI, 16 kHz).

Nothing slow ever runs in an audio callback or blocks the event loop:
DB writes go through a background queue; dashboard events are fire-and-forget.
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
import sounddevice as sd

from . import repo
from .audio.devices import AudioDeviceError, Device, find_device
from .audio.playback import PlaybackBuffer
from .audio.recorder import StereoRecorder
from .audio.resample import StreamResampler, rms, to_mono
from .config import Settings, get_settings
from .events import bus
from .prompts import CallContext, build_system_prompt
from .transcript import Segment, TranscriptAssembler
from .voice.base import NativeAudioRenderer, SpeechRenderer
from .voice.gemini_live import GeminiLiveBrain, LiveFatalError, LiveHandlers

log = logging.getLogger(__name__)
LIVE_IN_RATE = 16000
ONSET_GAP_S = 0.6          # silence needed before a new caller "onset" is registered
MAX_INPUT_BACKLOG_S = 0.5  # never send Gemini audio older than this


@dataclass
class CallOptions:
    mode: Literal["phone", "test"] = "test"
    direction: Literal["incoming", "outgoing"] = "incoming"
    contact_name: str = ""
    contact_phone: str = ""
    purpose: str = ""


class CallAgent:
    def __init__(self, opts: CallOptions, settings: Settings | None = None) -> None:
        self.opts = opts
        self.s = settings or get_settings()
        self.call_id: int | None = None
        self.state = "starting"
        self.started_at = time.monotonic()
        self.end_reason = ""
        self.error = ""
        self._stop = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._audio_in: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=max(2, int(MAX_INPUT_BACKLOG_S * 1000 / self.s.chunk_ms))
        )
        self._db_q: asyncio.Queue[Segment | None] = asyncio.Queue()
        self.recorder: StereoRecorder | None = None
        self.assembler = TranscriptAssembler(on_update=self._on_segment)
        # activity tracking (written from audio threads; floats are atomic under the GIL)
        self._last_caller_voice = 0.0
        self._last_ai_audio = 0.0
        self._last_event = time.monotonic()
        self._caller_onset_ms: int | None = None
        self._caller_level = 0.0
        self._ai_level = 0.0
        self._ai_playing = False
        self.last_latency_ms: int | None = None

    # ------------------------------------------------------------ public
    def request_stop(self, reason: str = "user") -> None:
        if not self.end_reason:
            self.end_reason = reason
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        else:
            self._stop.set()

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state, "call_id": self.call_id, "mode": self.opts.mode, "direction": self.opts.direction,
            "contact_name": self.opts.contact_name, "purpose": self.opts.purpose,
            "elapsed_s": round(time.monotonic() - self.started_at),
            "segments": [s.as_dict() for s in self.assembler.ordered(final_only=False)],
            "latency_ms": self.last_latency_ms,
        }

    async def run(self) -> int | None:
        self._loop = asyncio.get_running_loop()
        try:
            return await self._run()
        except AudioDeviceError as e:
            self.error = str(e)
            raise
        finally:
            if self.error:
                self._status("error", self.error)
            else:
                self._status("idle", f"Call ended ({self.end_reason or 'done'})")

    # ------------------------------------------------------------ internals
    def _now_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)

    def _status(self, state: str, message: str = "") -> None:
        self.state = state
        log.info("[call %s] %s %s", self.call_id, state, message)
        bus.publish({"type": "status", "state": state, "message": message, "call_id": self.call_id,
                     "mode": self.opts.mode, "direction": self.opts.direction, "contact_name": self.opts.contact_name})

    def _devices(self) -> tuple[Device, Device]:
        if self.opts.mode == "phone":
            return (find_device("input", self.s.caller_input_device, self.s.audio_hostapi),
                    find_device("output", self.s.ai_output_device, self.s.audio_hostapi))
        return (find_device("input", self.s.test_input_device, self.s.audio_hostapi),
                find_device("output", self.s.test_output_device, self.s.audio_hostapi))

    async def _run(self) -> int | None:
        s = self.s
        self._status("connecting", "Preparing call…")
        in_dev, out_dev = self._devices()  # fail fast before creating a DB row
        app = await repo.get_app_settings()
        recording = bool(app["recording_enabled"])
        voice = str(app["voice"])
        silence_timeout = int(app["silence_timeout_s"])

        facts, recent = await asyncio.gather(
            repo.list_facts(self.opts.contact_name), repo.recent_summaries(self.opts.contact_name)
        )
        prompt = build_system_prompt(
            owner_name=s.owner_name, languages=s.caller_languages, timezone=s.timezone,
            style_rules=str(app["style_rules"]), facts=facts, recent=recent,
            ctx=CallContext(self.opts.direction, self.opts.contact_name, self.opts.purpose, recording),
        )
        self.call_id = await repo.create_call(
            direction=self.opts.direction, mode=self.opts.mode, contact_name=self.opts.contact_name,
            contact_phone=self.opts.contact_phone, purpose=self.opts.purpose, voice=voice,
        )
        rec_path: Path | None = None
        if recording:
            rec_path = s.recordings_dir / f"call_{self.call_id}_{datetime.now():%Y%m%d_%H%M%S}.wav"
            self.recorder = StereoRecorder(rec_path, s.record_sample_rate)
            self.recorder.start()
            await repo.set_recording_path(self.call_id, str(rec_path))

        db_task = asyncio.create_task(self._db_writer(), name="db-writer")
        playback = PlaybackBuffer(out_dev.samplerate)
        renderer: SpeechRenderer = NativeAudioRenderer(out_dev.samplerate, playback.push)

        def on_interrupted() -> None:
            dropped = playback.clear()
            renderer.interrupt()
            self.assembler.interrupted(self._now_ms())
            self._last_event = time.monotonic()
            bus.publish({"type": "interrupted", "dropped_ms": round(dropped * 1000 / out_dev.samplerate)})

        def on_input_text(text: str, finished: bool) -> None:
            self._last_event = time.monotonic()
            self.assembler.caller_text(text, self._now_ms(), self._caller_onset_ms, finished)

        def on_output_text(text: str) -> None:
            self._last_event = time.monotonic()
            renderer.on_model_text(text)
            self.assembler.assistant_text(text, self._now_ms())

        def on_turn_complete() -> None:
            self.assembler.turn_complete(self._now_ms())

        brain = GeminiLiveBrain(
            api_key=s.gemini_api_key, model=s.live_model, voice=voice, system_prompt=prompt,
            handlers=LiveHandlers(
                on_audio=renderer.on_model_audio, on_input_text=on_input_text, on_output_text=on_output_text,
                on_interrupted=on_interrupted, on_turn_complete=on_turn_complete,
                on_status=lambda st, m: self._status(st, m), transcript_so_far=self.assembler.as_text,
            ),
            vad_start=s.vad_start_sensitivity, vad_end=s.vad_end_sensitivity, vad_silence_ms=s.vad_silence_ms,
        )

        in_stream = out_stream = None
        try:
            in_stream, out_stream = self._open_streams(in_dev, out_dev, playback)
            out_stream.start()
            in_stream.start()
            bus.publish({"type": "devices", "input": f"{in_dev.name} ({in_dev.hostapi})",
                         "output": f"{out_dev.name} ({out_dev.hostapi})"})
            brain_task = asyncio.create_task(brain.run(self._audio_in, self._stop), name="live-brain")
            watch_task = asyncio.create_task(self._watchdog(silence_timeout, playback), name="watchdog")
            done, _ = await asyncio.wait({brain_task, watch_task}, return_when=asyncio.FIRST_COMPLETED)
            self._stop.set()
            for t in (brain_task, watch_task):
                if not t.done():
                    t.cancel()
            await asyncio.gather(brain_task, watch_task, return_exceptions=True)
            if brain_task.done() and not brain_task.cancelled() and brain_task.exception():
                raise brain_task.exception()  # type: ignore[misc]
        except LiveFatalError as e:
            self.error, self.end_reason = str(e), self.end_reason or "error"
            self._status("error", str(e))
        except sd.PortAudioError as e:
            self.error, self.end_reason = f"Audio device error: {e}. Run `python -m app.devices` to check devices.", "error"
            self._status("error", self.error)
        except asyncio.CancelledError:
            self.end_reason = self.end_reason or "cancelled"
            raise
        except Exception as e:
            log.exception("call failed")
            self.error, self.end_reason = f"Unexpected error: {e}", self.end_reason or "error"
            self._status("error", self.error)
        finally:
            # Always persist whatever we have, even after errors.
            self._status_quiet("ending")
            for st in (in_stream, out_stream):
                if st is not None:
                    try:
                        st.stop()
                        st.close()
                    except Exception:
                        pass
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
            bus.publish({"type": "call_saved", "call_id": self.call_id, "reconnects": brain.reconnects})
        return self.call_id

    def _status_quiet(self, state: str) -> None:
        self.state = state
        bus.publish({"type": "status", "state": state, "message": "Saving call…", "call_id": self.call_id})

    # ------------------------------------------------------------ audio
    def _open_streams(self, in_dev: Device, out_dev: Device, playback: PlaybackBuffer) -> tuple[sd.InputStream, sd.OutputStream]:
        s, loop = self.s, self._loop
        assert loop is not None
        rec = self.recorder
        in_res = StreamResampler(in_dev.samplerate, LIVE_IN_RATE, quality="MQ")
        out_rec_res = StreamResampler(out_dev.samplerate, s.record_sample_rate, quality="MQ")
        caller_rec_res = (StreamResampler(LIVE_IN_RATE, s.record_sample_rate, quality="MQ")
                          if s.record_sample_rate != LIVE_IN_RATE else None)
        thr = s.silence_rms_threshold
        q = self._audio_in

        def enqueue(b: bytes) -> None:
            if q.full():  # drop oldest: low latency beats completeness
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(b)

        def in_cb(indata: np.ndarray, frames: int, t: Any, status: sd.CallbackFlags) -> None:
            pcm = in_res.process(to_mono(indata))
            if pcm.size == 0:
                return
            now = time.monotonic()
            level = rms(pcm)
            self._caller_level = level
            if level > thr:
                if now - self._last_caller_voice > ONSET_GAP_S:
                    self._caller_onset_ms = int((now - self.started_at) * 1000)
                self._last_caller_voice = now
            if rec:
                rec.write_caller(caller_rec_res.process(pcm) if caller_rec_res else pcm, now)
            loop.call_soon_threadsafe(enqueue, pcm.tobytes())

        channels_out = min(2, out_dev.max_channels)

        def out_cb(outdata: np.ndarray, frames: int, t: Any, status: sd.CallbackFlags) -> None:
            pcm, real = playback.read(frames)
            outdata[:] = pcm.reshape(-1, 1) if channels_out == 1 else np.repeat(pcm.reshape(-1, 1), channels_out, axis=1)
            now = time.monotonic()
            if real:
                if not self._ai_playing:
                    self._ai_playing = True
                    # response latency = caller stopped talking -> first AI sound out of the speaker
                    gap = now - self._last_caller_voice
                    if 0 < gap < 5:
                        self.last_latency_ms = int(gap * 1000)
                        bus.publish_threadsafe({"type": "latency", "ms": self.last_latency_ms})
                self._last_ai_audio = now
                self._ai_level = rms(pcm[:real])
            else:
                self._ai_playing = False
                self._ai_level = 0.0
            if rec:
                rec.write_ai(out_rec_res.process(pcm), now)

        bs_in = in_dev.samplerate * s.chunk_ms // 1000
        bs_out = out_dev.samplerate * s.chunk_ms // 1000
        try:
            ins = sd.InputStream(device=in_dev.index, samplerate=in_dev.samplerate, channels=min(2, in_dev.max_channels),
                                 dtype="int16", blocksize=bs_in, latency="low", callback=in_cb)
            outs = sd.OutputStream(device=out_dev.index, samplerate=out_dev.samplerate, channels=channels_out,
                                   dtype="int16", blocksize=bs_out, latency="low", callback=out_cb)
        except sd.PortAudioError as e:
            raise AudioDeviceError(
                f"Could not open audio device ({e}). Check it isn't in exclusive use by another app, "
                "or try AUDIO_HOSTAPI=Windows DirectSound in .env. Run `python -m app.devices` to list devices."
            ) from e
        return ins, outs

    # ------------------------------------------------------------ background tasks
    async def _watchdog(self, silence_timeout: int, playback: PlaybackBuffer) -> None:
        """Ends the call after `silence_timeout` s with no speech from either side; streams level meters."""
        tick = 0
        while not self._stop.is_set():
            await asyncio.sleep(0.2)
            tick += 1
            now = time.monotonic()
            bus.publish({"type": "levels", "caller": round(min(1.0, self._caller_level / 4000), 3),
                         "ai": round(min(1.0, self._ai_level / 4000), 3), "buffer_ms": round(playback.buffered_ms)})
            last = max(self._last_caller_voice, self._last_ai_audio, self._last_event, self.started_at)
            if playback.buffered_ms < 1 and now - last > silence_timeout:
                self.end_reason = self.end_reason or "silence"
                log.info("silence timeout (%ss)", silence_timeout)
                return

    def _on_segment(self, seg: Segment) -> None:
        bus.publish({"type": "segment", "call_id": self.call_id, **seg.as_dict()})
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
