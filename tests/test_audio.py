import wave
from pathlib import Path

import numpy as np

from app.audio.playback import PlaybackBuffer
from app.audio.recorder import StereoRecorder, align_tracks, finalize_orphan
from app.audio.resample import StreamResampler, resample, rms


def tone(freq: float, rate: int, seconds: float, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (np.sin(2 * np.pi * freq * t) * amp * 32767).astype(np.int16)


def dominant_freq(x: np.ndarray, rate: int) -> float:
    spec = np.abs(np.fft.rfft(x.astype(np.float32)))
    return float(np.argmax(spec) * rate / x.size)


def test_one_shot_resample_24k_to_16k_keeps_pitch_and_length():
    x = tone(440, 24000, 1.0)
    y = resample(x, 24000, 16000)
    assert abs(y.size - 16000) <= 2
    assert abs(dominant_freq(y, 16000) - 440) < 3


def test_stream_resampler_chunked_matches_length_and_pitch():
    x = tone(1000, 48000, 1.0)
    rs = StreamResampler(48000, 16000)
    out = np.concatenate([rs.process(x[i:i + 960]) for i in range(0, x.size, 960)] + [rs.process(np.zeros(0, np.int16), last=True)])
    assert abs(out.size - 16000) <= 64
    assert abs(dominant_freq(out, 16000) - 1000) < 5


def test_stream_resampler_passthrough():
    x = tone(300, 16000, 0.1)
    assert np.array_equal(StreamResampler(16000, 16000).process(x), x)


def test_playback_buffer_read_pad_and_clear():
    pb = PlaybackBuffer(16000)
    pb.push(np.arange(100, dtype=np.int16))
    out, real = pb.read(60)
    assert real == 60 and out[59] == 59
    out, real = pb.read(60)
    assert real == 40 and out[39] == 99 and out[40] == 0
    pb.push(np.ones(500, np.int16))
    gen = pb.generation
    assert pb.clear() == 500 and pb.buffered_ms == 0 and pb.generation == gen + 1
    pb.push(np.ones(10, np.int16), generation=gen)  # stale producer is ignored
    assert pb.buffered_ms == 0


def test_playback_buffer_is_bounded():
    pb = PlaybackBuffer(1000, max_seconds=1)
    for _ in range(5):
        pb.push(np.ones(500, np.int16))
    assert pb.buffered_ms <= 1000


def test_align_tracks_pads_later_track_front():
    l, r = align_tracks(np.ones(100, np.int16), np.ones(50, np.int16), 10.0, 10.25, 100)
    assert l.size == r.size == 100
    assert r[:25].sum() == 0 and r[25:75].sum() == 50


def test_recorder_writes_aligned_stereo_wav(tmp_path: Path):
    rate = 16000
    wav = tmp_path / "call.wav"
    rec = StereoRecorder(wav, rate)
    rec.start()
    caller, ai = tone(300, rate, 1.0), tone(1200, rate, 0.5)
    t0 = 100.0
    for i in range(0, caller.size, 320):  # 20 ms blocks
        rec.write_caller(caller[i:i + 320], t0 + (i + 320) / rate)
    # AI starts 0.5 s after the caller
    for i in range(0, ai.size, 320):
        rec.write_ai(ai[i:i + 320], t0 + 0.5 + (i + 320) / rate)
    out = rec.finalize()
    assert out == wav and wav.exists()
    assert not wav.with_suffix(".caller.pcm").exists()
    with wave.open(str(wav)) as w:
        assert w.getnchannels() == 2 and w.getframerate() == rate
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").reshape(-1, 2)
    left, right = data[:, 0], data[:, 1]
    assert abs(left.size - rate) <= 2
    assert rms(right[: rate // 2 - 50]) < 1           # AI silent during first half
    assert abs(dominant_freq(left, rate) - 300) < 3
    assert abs(dominant_freq(right[rate // 2:], rate) - 1200) < 5
    assert rec.finalize() == wav  # idempotent


def test_finalize_orphan_recovers_after_crash(tmp_path: Path):
    wav = tmp_path / "crashed.wav"
    rec = StereoRecorder(wav, 16000)
    rec.start()
    rec.write_caller(tone(500, 16000, 0.2), 1.0)
    rec.write_ai(tone(800, 16000, 0.2), 1.0)
    rec._q.put(__import__("app.audio.recorder", fromlist=["_STOP"])._STOP)  # simulate thread end without finalize
    rec._thread.join()  # type: ignore[union-attr]
    assert not wav.exists()
    assert finalize_orphan(wav) == wav
    with wave.open(str(wav)) as w:
        assert w.getnchannels() == 2
