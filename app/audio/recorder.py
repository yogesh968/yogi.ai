"""Crash-tolerant stereo call recorder: left = caller, right = AI, time-aligned.

Audio callbacks hand blocks to a writer thread which appends to two raw PCM
side files. finalize() merges them into a stereo WAV. If the process dies
mid-call, finalize_orphan() can rebuild the WAV from the side files on restart.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)
_STOP = object()


def _side_paths(wav: Path) -> tuple[Path, Path, Path]:
    return wav.with_suffix(".caller.pcm"), wav.with_suffix(".ai.pcm"), wav.with_suffix(".meta.json")


class StereoRecorder:
    def __init__(self, wav_path: Path, rate: int) -> None:
        self.wav_path = wav_path
        self.rate = rate
        self._caller_p, self._ai_p, self._meta_p = _side_paths(wav_path)
        self._q: queue.SimpleQueue[object] = queue.SimpleQueue()
        self._t0: dict[str, float] = {}
        self._thread: threading.Thread | None = None
        self._finalized = False

    # -- called from audio threads: must never block
    def write_caller(self, pcm16: np.ndarray, t: float | None = None) -> None:
        self._q.put(("caller", pcm16.copy(), t or time.monotonic()))

    def write_ai(self, pcm16: np.ndarray, t: float | None = None) -> None:
        self._q.put(("ai", pcm16.copy(), t or time.monotonic()))

    def start(self) -> None:
        self.wav_path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        with open(self._caller_p, "ab") as fc, open(self._ai_p, "ab") as fa:
            files = {"caller": fc, "ai": fa}
            while True:
                item = self._q.get()
                if item is _STOP:
                    break
                track, pcm, t = item  # type: ignore[misc]
                if track not in self._t0:
                    # Timestamp of the block's first sample = callback time - block duration.
                    self._t0[track] = t - pcm.size / self.rate
                    self._meta_p.write_text(json.dumps({"rate": self.rate, "t0": self._t0}))
                files[track].write(pcm.astype("<i2").tobytes())

    def finalize(self) -> Path | None:
        """Stop writing and produce the stereo WAV. Safe to call more than once."""
        if self._finalized:
            return self.wav_path if self.wav_path.exists() else None
        self._finalized = True
        if self._thread:
            self._q.put(_STOP)
            self._thread.join(timeout=10)
        return finalize_orphan(self.wav_path)


def finalize_orphan(wav_path: Path) -> Path | None:
    """Merge side files into a stereo WAV aligned by their start timestamps. Deletes side files on success."""
    caller_p, ai_p, meta_p = _side_paths(wav_path)
    if not caller_p.exists() and not ai_p.exists():
        return wav_path if wav_path.exists() else None
    try:
        meta = json.loads(meta_p.read_text()) if meta_p.exists() else {"rate": 16000, "t0": {}}
        rate = int(meta["rate"])
        t0: dict[str, float] = meta.get("t0", {})
        left = np.fromfile(caller_p, dtype="<i2") if caller_p.exists() else np.zeros(0, np.int16)
        right = np.fromfile(ai_p, dtype="<i2") if ai_p.exists() else np.zeros(0, np.int16)
        left, right = align_tracks(left, right, t0.get("caller"), t0.get("ai"), rate)
        stereo = np.empty(left.size * 2, dtype="<i2")
        stereo[0::2], stereo[1::2] = left, right
        tmp = wav_path.with_suffix(".wav.tmp")
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(stereo.tobytes())
        tmp.replace(wav_path)
        for p in (caller_p, ai_p, meta_p):
            p.unlink(missing_ok=True)
        return wav_path
    except Exception:
        log.exception("could not finalize recording %s", wav_path)
        return None


def align_tracks(left: np.ndarray, right: np.ndarray, t_left: float | None, t_right: float | None, rate: int) -> tuple[np.ndarray, np.ndarray]:
    """Pad the later-starting track at the front, then pad both to equal length."""
    if t_left is not None and t_right is not None:
        offset = int(round((t_right - t_left) * rate))
        if offset > 0:
            right = np.concatenate([np.zeros(offset, np.int16), right])
        elif offset < 0:
            left = np.concatenate([np.zeros(-offset, np.int16), left])
    n = max(left.size, right.size)
    left = np.pad(left, (0, n - left.size))
    right = np.pad(right, (0, n - right.size))
    return left.astype(np.int16, copy=False), right.astype(np.int16, copy=False)
