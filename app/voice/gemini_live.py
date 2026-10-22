"""Gemini Live session that survives connection rotation (GoAway) and drops.

- Session resumption: we keep the latest resumable handle and reconnect with it,
  so the model keeps full conversation state. Handles are valid ~2 h.
- Context window compression (sliding window) lifts the 15-min audio session cap.
- If a reconnect happens with no handle, we re-seed the transcript so far.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable

from google import genai
from google.genai import types

from ..prompts import GREETING_TRIGGER, resume_context_prompt

log = logging.getLogger(__name__)
INPUT_MIME = "audio/pcm;rate=16000"


class LiveFatalError(RuntimeError):
    """Not worth retrying (bad key, bad model name, quota)."""


@dataclass
class LiveHandlers:
    on_audio: Callable[[bytes], None]
    on_input_text: Callable[[str, bool], None]
    on_output_text: Callable[[str], None]
    on_interrupted: Callable[[], None]
    on_turn_complete: Callable[[], None]
    on_status: Callable[[str, str], None]          # (state, message)
    transcript_so_far: Callable[[], str]


_SENS_START = {"high": types.StartSensitivity.START_SENSITIVITY_HIGH, "low": types.StartSensitivity.START_SENSITIVITY_LOW}
_SENS_END = {"high": types.EndSensitivity.END_SENSITIVITY_HIGH, "low": types.EndSensitivity.END_SENSITIVITY_LOW}


def friendly_live_error(e: BaseException) -> tuple[str, bool]:
    """(message, fatal)"""
    m = str(e)
    low = m.lower()
    if "api key" in low or "api_key" in low or "permission" in low or "unauthenticated" in low or "401" in m or "403" in m:
        return "Gemini rejected the API key. Check GEMINI_API_KEY in .env (create one at aistudio.google.com/apikey).", True
    if ("not found" in low and "model" in low) or "is not supported" in low or "unsupported" in low:
        return (f"Gemini says the Live model isn't available: {m[:200]}. Check LIVE_MODEL in .env against "
                "ai.google.dev/gemini-api/docs/models (currently gemini-3.8-live)."), True
    if "quota" in low or "resource_exhausted" in low or "429" in m:
        return "Gemini free-tier quota/rate limit hit. Wait a minute and try again, or check usage in AI Studio.", True
    if "invalid" in low and ("argument" in low or "config" in low or "1007" in m):
        return f"Gemini rejected the session config: {m[:300]}", True
    return f"Live connection error: {m[:300]}", False


def _parse_seconds(s: str | None) -> float:
    if not s:
        return 0.0
    mt = re.match(r"^\s*([\d.]+)\s*s?\s*$", s)
    return float(mt.group(1)) if mt else 0.0


class GeminiLiveBrain:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        voice: str,
        system_prompt: str,
        handlers: LiveHandlers,
        vad_start: str,
        vad_end: str,
        vad_silence_ms: int,
        greet_first: bool = True,
    ) -> None:
        self._client = genai.Client(api_key=api_key)
        self.model = model
        self.voice = voice
        self.system_prompt = system_prompt
        self.h = handlers
        self.vad = (vad_start, vad_end, vad_silence_ms)
        self.handle: str | None = None
        self._greet_pending = greet_first
        self._connected_once = False
        self.reconnects = 0

    def _config(self) -> types.LiveConnectConfig:
        start, end, silence_ms = self.vad
        return types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=self.system_prompt,
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice))
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            session_resumption=types.SessionResumptionConfig(handle=self.handle),
            context_window_compression=types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow()),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=False,
                    start_of_speech_sensitivity=_SENS_START.get(start),
                    end_of_speech_sensitivity=_SENS_END.get(end),
                    prefix_padding_ms=40,
                    silence_duration_ms=silence_ms,
                )
            ),
        )

    async def run(self, audio_in: asyncio.Queue[bytes], stop: asyncio.Event) -> None:
        """Runs until `stop` is set. Raises LiveFatalError on unrecoverable errors."""
        failures = 0
        while not stop.is_set():
            resumed = self.handle is not None
            try:
                async with self._client.aio.live.connect(model=self.model, config=self._config()) as session:
                    failures = 0
                    if self._connected_once:
                        self.reconnects += 1
                        self.h.on_status("live", "Reconnected" + (" (session resumed)" if resumed else ""))
                    else:
                        self.h.on_status("live", "Connected to Gemini Live")
                    await self._prime(session, resumed)
                    self._connected_once = True
                    await self._pump(session, audio_in, stop)
            except asyncio.CancelledError:
                raise
            except LiveFatalError:
                raise
            except Exception as e:
                msg, fatal = friendly_live_error(e)
                failures += 1
                log.warning("live session error (attempt %d): %s", failures, e)
                if fatal or (not self._connected_once and failures >= 2) or failures > 6:
                    raise LiveFatalError(msg) from e
                if not stop.is_set():
                    self.h.on_status("reconnecting", msg)
                    await asyncio.sleep(min(0.2 * 2 ** failures, 3.0))
                continue
            if not stop.is_set():
                self.h.on_status("reconnecting", "Rotating connection…")

    async def _prime(self, session: genai.live.AsyncSession, resumed: bool) -> None:
        if self._greet_pending:
            self._greet_pending = False
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=GREETING_TRIGGER)]), turn_complete=True
            )
        elif self._connected_once and not resumed:
            # Lost state (no resumption handle): give the model the conversation so far.
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=resume_context_prompt(self.h.transcript_so_far()))]),
                turn_complete=False,
            )

    async def _pump(self, session: genai.live.AsyncSession, audio_in: asyncio.Queue[bytes], stop: asyncio.Event) -> None:
        rotate = asyncio.Event()
        tasks = {
            asyncio.create_task(self._send_loop(session, audio_in), name="live-send"),
            asyncio.create_task(self._recv_loop(session, rotate), name="live-recv"),
            asyncio.create_task(stop.wait(), name="stop"),
            asyncio.create_task(rotate.wait(), name="rotate"),
        }
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.get_name() in ("live-send", "live-recv") and t.exception():
                    raise t.exception()  # type: ignore[misc]
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_loop(self, session: genai.live.AsyncSession, audio_in: asyncio.Queue[bytes]) -> None:
        while True:
            chunk = await audio_in.get()
            await session.send_realtime_input(audio=types.Blob(data=chunk, mime_type=INPUT_MIME))

    async def _recv_loop(self, session: genai.live.AsyncSession, rotate: asyncio.Event) -> None:
        go_away_deadline: float | None = None
        while True:
            got_any = False
            # receive() ends after each model turn, so it must be re-entered.
            async for msg in session.receive():
                got_any = True
                upd = msg.session_resumption_update
                if upd and upd.resumable and upd.new_handle:
                    self.handle = upd.new_handle

                if msg.go_away is not None:
                    left = _parse_seconds(msg.go_away.time_left)
                    log.info("GoAway received, %.1fs left", left)
                    go_away_deadline = time.monotonic() + max(0.0, left - 2.0)
                    asyncio.get_running_loop().call_later(max(0.0, left - 2.0), rotate.set)

                sc = msg.server_content
                if sc is None:
                    continue
                if sc.interrupted:
                    self.h.on_interrupted()
                if sc.model_turn and sc.model_turn.parts:
                    for part in sc.model_turn.parts:
                        if part.inline_data and part.inline_data.data:
                            self.h.on_audio(part.inline_data.data)
                if sc.input_transcription and (sc.input_transcription.text or sc.input_transcription.finished):
                    self.h.on_input_text(sc.input_transcription.text or "", bool(sc.input_transcription.finished))
                if sc.output_transcription and sc.output_transcription.text:
                    self.h.on_output_text(sc.output_transcription.text)
                if sc.turn_complete:
                    self.h.on_turn_complete()
                    if go_away_deadline is not None:
                        rotate.set()  # good moment to switch: model just finished speaking
                        return
            if not got_any:
                return  # socket closed: outer loop reconnects
