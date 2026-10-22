"""Local test transport: your computer's mic + headphones (PortAudio callbacks)."""
from __future__ import annotations

import time
from typing import Any

import numpy as np

from ..audio.devices import AudioDeviceError, Device, find_device
from ..audio.playback import PlaybackBuffer
from ..audio.resample import StreamResampler, to_mono
from ..config import Settings
from .base import CALLER_RATE, AudioSink, AudioTransport


def local_audio_available() -> bool:
    try:
        import sounddevice  # noqa: F401  # needs the PortAudio library, absent on headless servers
        return True
    except (ImportError, OSError):
        return False


class LocalAudioTransport(AudioTransport):
    def __init__(self, s: Settings) -> None:
        super().__init__()
        if not local_audio_available():
            raise AudioDeviceError("No audio devices on this machine (sounddevice/PortAudio not available). "
                                   "Local test mode only works on your computer.")
        self.s = s
        self.in_dev: Device = find_device("input", s.test_input_device, s.audio_hostapi)
        self.out_dev: Device = find_device("output", s.test_output_device, s.audio_hostapi)
        self.output_rate = self.out_dev.samplerate
        self.playback = PlaybackBuffer(self.output_rate)
        self._streams: list[Any] = []

    def describe(self) -> dict[str, str]:
        return {"input": f"{self.in_dev.name} ({self.in_dev.hostapi})", "output": f"{self.out_dev.name} ({self.out_dev.hostapi})"}

    async def start(self, sink: AudioSink) -> None:
        import sounddevice as sd

        in_res = StreamResampler(self.in_dev.samplerate, CALLER_RATE, quality="LQ")
        channels_out = min(2, self.out_dev.max_channels)
        playback = self.playback

        def in_cb(indata: np.ndarray, frames: int, t: Any, status: Any) -> None:
            pcm = in_res.process(to_mono(indata))
            if pcm.size:
                sink.caller_audio(pcm, time.monotonic())

        def out_cb(outdata: np.ndarray, frames: int, t: Any, status: Any) -> None:
            pcm, real = playback.read(frames)
            outdata[:] = np.repeat(pcm.reshape(-1, 1), channels_out, axis=1)
            sink.ai_played(pcm, real, time.monotonic())

        chunk = self.s.chunk_ms
        try:
            ins = sd.InputStream(device=self.in_dev.index, samplerate=self.in_dev.samplerate,
                                 channels=min(2, self.in_dev.max_channels), dtype="int16",
                                 blocksize=self.in_dev.samplerate * chunk // 1000, latency="low", callback=in_cb)
            outs = sd.OutputStream(device=self.out_dev.index, samplerate=self.out_dev.samplerate, channels=channels_out,
                                   dtype="int16", blocksize=self.out_dev.samplerate * chunk // 1000, latency="low",
                                   callback=out_cb)
        except sd.PortAudioError as e:
            raise AudioDeviceError(
                f"Could not open audio device ({e}). Close apps using it exclusively, or set "
                "AUDIO_HOSTAPI=Windows DirectSound in .env. Run `python -m app.devices` to list devices."
            ) from e
        self._streams = [outs, ins]
        outs.start()
        ins.start()

    async def stop(self) -> None:
        for st in self._streams:
            try:
                st.stop()
                st.close()
            except Exception:
                pass
        self._streams = []
