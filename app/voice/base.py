"""Speech rendering interface.

Today the Gemini Live model speaks with its own native audio (NativeAudioRenderer).
A cloned-voice pipeline later only needs a new renderer that takes the model's
output *text* (output transcription) and synthesizes PCM, e.g.:

    class ClonedVoiceRenderer(SpeechRenderer):
        uses_native_audio = False
        def on_model_text(self, text): stream text into your TTS, push PCM to `sink`

The agent never cares which renderer is active.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

import numpy as np

from ..audio.resample import StreamResampler

GEMINI_OUTPUT_RATE = 24000
PcmSink = Callable[[np.ndarray], None]  # receives int16 mono at the playback device rate


class SpeechRenderer(ABC):
    uses_native_audio: bool = True

    def __init__(self, device_rate: int, sink: PcmSink) -> None:
        self.device_rate = device_rate
        self.sink = sink

    @abstractmethod
    def on_model_audio(self, pcm: bytes) -> None: ...

    def on_model_text(self, text: str) -> None:  # noqa: B027 - optional hook
        pass

    def interrupt(self) -> None:  # noqa: B027 - optional hook
        pass


class NativeAudioRenderer(SpeechRenderer):
    """Plays Gemini's native 24 kHz voice, resampled to the output device rate."""

    def __init__(self, device_rate: int, sink: PcmSink) -> None:
        super().__init__(device_rate, sink)
        self._rs = StreamResampler(GEMINI_OUTPUT_RATE, device_rate, quality="MQ")

    def on_model_audio(self, pcm: bytes) -> None:
        x = np.frombuffer(pcm, dtype="<i2")
        if x.size:
            self.sink(self._rs.process(x))

    def interrupt(self) -> None:
        # Fresh resampler so no tail of the interrupted sentence leaks into the next one.
        self._rs = StreamResampler(GEMINI_OUTPUT_RATE, self.device_rate, quality="MQ")
