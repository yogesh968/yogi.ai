"""Audio transport interface: how caller audio reaches the agent and AI audio reaches the caller."""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Protocol

import numpy as np

from ..audio.playback import PlaybackBuffer

CALLER_RATE = 16000  # every transport delivers caller audio as 16 kHz mono int16 (Gemini Live's input rate)


class AudioSink(Protocol):
    """Implemented by the agent. Both methods must be safe to call from any thread and must never block."""

    def caller_audio(self, pcm16k: np.ndarray, t: float) -> None: ...

    def ai_played(self, pcm: np.ndarray, real: int, t: float) -> None:
        """`pcm` = audio actually sent to the caller at `output_rate` (zero-padded); `real` = non-padding samples."""


class AudioTransport(ABC):
    output_rate: int
    playback: PlaybackBuffer
    can_hang_up: bool = False

    def __init__(self) -> None:
        self.closed = asyncio.Event()  # set when the far end goes away (caller hung up / device lost)

    @abstractmethod
    async def start(self, sink: AudioSink) -> None: ...

    @abstractmethod
    async def stop(self) -> None:
        """Release resources. Idempotent."""

    def interrupt(self) -> int:
        """Caller barged in: drop queued AI audio immediately. Returns samples dropped."""
        return self.playback.clear()

    async def hang_up(self) -> None:
        """End the phone call itself, if this transport can."""

    @abstractmethod
    def describe(self) -> dict[str, str]: ...
