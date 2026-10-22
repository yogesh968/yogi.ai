"""Turns streaming transcription fragments into ordered, speaker-labelled segments.

Gemini's input (caller) transcription can arrive *after* the model has started
replying, so segments are ordered by when speech actually began (from local
voice-activity onset), not by when the text arrived.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

Speaker = str  # "caller" | "assistant"
_WS = re.compile(r"[ \t]+")


@dataclass
class Segment:
    seq: int
    speaker: Speaker
    start_ms: int
    end_ms: int
    text: str = ""
    final: bool = False
    interrupted: bool = False

    def as_dict(self) -> dict[str, object]:
        return {"seq": self.seq, "speaker": self.speaker, "text": self.text, "start_ms": self.start_ms,
                "end_ms": self.end_ms, "final": self.final, "interrupted": self.interrupted}


@dataclass
class TranscriptAssembler:
    on_update: Callable[[Segment], None] = lambda s: None
    segments: list[Segment] = field(default_factory=list)
    _open: dict[Speaker, Segment] = field(default_factory=dict)
    _next_seq: int = 0

    def _start(self, speaker: Speaker, start_ms: int) -> Segment:
        seg = Segment(self._next_seq, speaker, start_ms, start_ms)
        self._next_seq += 1
        self.segments.append(seg)
        self._open[speaker] = seg
        return seg

    def _close(self, speaker: Speaker, now_ms: int, interrupted: bool = False) -> None:
        seg = self._open.pop(speaker, None)
        if seg is None:
            return
        seg.text = seg.text.strip()
        seg.final = True
        seg.interrupted = interrupted
        if not seg.text:
            self.segments.remove(seg)  # nothing was said
            return
        self.on_update(seg)

    @staticmethod
    def _append(seg: Segment, fragment: str) -> None:
        seg.text = _WS.sub(" ", seg.text + fragment)

    def caller_text(self, fragment: str, now_ms: int, onset_ms: int | None = None, finished: bool = False) -> None:
        if fragment:
            seg = self._open.get("caller")
            if seg is None:
                start = onset_ms if onset_ms is not None and onset_ms <= now_ms else now_ms
                seg = self._start("caller", start)
            self._append(seg, fragment)
            seg.end_ms = now_ms
            self.on_update(seg)
        if finished:
            self._close("caller", now_ms)

    def assistant_text(self, fragment: str, now_ms: int) -> None:
        if not fragment:
            return
        # The model only replies once the caller's turn is over.
        self._close("caller", now_ms)
        seg = self._open.get("assistant") or self._start("assistant", now_ms)
        self._append(seg, fragment)
        seg.end_ms = now_ms
        self.on_update(seg)

    def turn_complete(self, now_ms: int) -> None:
        self._close("assistant", now_ms)

    def interrupted(self, now_ms: int) -> None:
        self._close("assistant", now_ms, interrupted=True)

    def close_all(self, now_ms: int) -> None:
        for sp in list(self._open):
            self._close(sp, now_ms)

    def ordered(self, final_only: bool = True) -> list[Segment]:
        segs = [s for s in self.segments if s.final or not final_only]
        return sorted(segs, key=lambda s: (s.start_ms, s.seq))

    def as_text(self) -> str:
        return "\n".join(f"{s.speaker.capitalize()}: {s.text}" for s in self.ordered())
