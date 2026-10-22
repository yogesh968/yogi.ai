"""Thread-safe playback FIFO read by the PortAudio output callback."""
from __future__ import annotations

import threading
from collections import deque

import numpy as np


class PlaybackBuffer:
    def __init__(self, rate: int, max_seconds: float = 120.0) -> None:
        self.rate = rate
        self._max = int(rate * max_seconds)
        self._chunks: deque[np.ndarray] = deque()
        self._size = 0
        self._lock = threading.Lock()
        self._generation = 0  # bumped on clear() so in-flight producers can drop stale audio

    @property
    def generation(self) -> int:
        return self._generation

    def push(self, pcm: np.ndarray, generation: int | None = None) -> None:
        if pcm.size == 0:
            return
        with self._lock:
            if generation is not None and generation != self._generation:
                return  # audio from a turn that was interrupted
            self._chunks.append(pcm.astype(np.int16, copy=False))
            self._size += pcm.size
            while self._size > self._max and self._chunks:  # safety valve, never grows unbounded
                self._size -= self._chunks.popleft().size

    def read(self, n: int) -> tuple[np.ndarray, int]:
        """Return exactly n samples (zero-padded) and how many were real audio."""
        out = np.zeros(n, dtype=np.int16)
        filled = 0
        with self._lock:
            while filled < n and self._chunks:
                c = self._chunks[0]
                take = min(n - filled, c.size)
                out[filled:filled + take] = c[:take]
                filled += take
                if take == c.size:
                    self._chunks.popleft()
                else:
                    self._chunks[0] = c[take:]
            self._size -= filled
        return out, filled

    def clear(self) -> int:
        """Flush instantly (caller barged in). Returns samples dropped."""
        with self._lock:
            dropped = self._size
            self._chunks.clear()
            self._size = 0
            self._generation += 1
        return dropped

    @property
    def buffered_ms(self) -> float:
        return self._size * 1000.0 / self.rate
