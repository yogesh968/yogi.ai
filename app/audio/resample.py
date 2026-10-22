"""Streaming resampling and small DSP helpers (all mono int16 unless noted)."""
from __future__ import annotations

import numpy as np
import soxr


class StreamResampler:
    """Stateful resampler for continuous chunked audio (no clicks at chunk boundaries)."""

    def __init__(self, in_rate: int, out_rate: int, quality: str = "HQ") -> None:
        self.in_rate, self.out_rate = in_rate, out_rate
        self._passthrough = in_rate == out_rate
        self._rs = None if self._passthrough else soxr.ResampleStream(in_rate, out_rate, 1, dtype="float32", quality=quality)

    def process(self, x: np.ndarray, last: bool = False) -> np.ndarray:
        """int16 or float32 mono in -> int16 mono out."""
        if self._passthrough:
            return to_int16(x)
        f = x.astype(np.float32) / 32768.0 if x.dtype == np.int16 else x.astype(np.float32, copy=False)
        return to_int16(self._rs.resample_chunk(f, last=last))  # type: ignore[union-attr]


def resample(x: np.ndarray, in_rate: int, out_rate: int) -> np.ndarray:
    """One-shot resample of a whole int16 mono signal."""
    if in_rate == out_rate:
        return x.astype(np.int16, copy=False)
    y = soxr.resample(x.astype(np.float32) / 32768.0, in_rate, out_rate, quality="HQ")
    return to_int16(y)


def to_int16(x: np.ndarray) -> np.ndarray:
    if x.dtype == np.int16:
        return x
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16)


def to_mono(block: np.ndarray) -> np.ndarray:
    """(frames, ch) -> (frames,). Uses channel 0: virtual cables deliver identical L/R and averaging costs time."""
    return block[:, 0] if block.ndim == 2 else block


def rms(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    f = x.astype(np.float32)
    if x.dtype != np.int16:
        f *= 32768.0
    return float(np.sqrt(np.mean(f * f)))
