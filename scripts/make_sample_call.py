"""Create a realistic sample call (real synthesized speech, stereo WAV) and queue it for the worker.

    python -m scripts.make_sample_call

Uses Gemini TTS to voice a short Hinglish conversation: caller on the left channel, assistant on the right.
Marked as a 'test' call so it's clearly distinguishable in History.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import numpy as np
from google import genai
from google.genai import types

from app import db, repo
from app.audio.recorder import StereoRecorder
from app.audio.resample import resample
from app.config import get_settings
from app.migrate import migrate

TTS_MODEL = "gemini-3.8-flash-tts"
SCRIPT = [
    ("assistant", "Puck", "Hi, this is {owner}'s AI assistant, and this call is being recorded. {owner} can't take the call right now. How can I help?"),
    ("caller", "Kore", "Haan hi, main Priya bol rahi hoon, from the design team. Kal ki client meeting ke baare mein call kiya tha."),
    ("assistant", "Puck", "Achha, theek hai. What would you like me to tell {owner} about the meeting?"),
    ("caller", "Kore", "Bas itna batana ki meeting ab Thursday ko four pm pe shift ho gayi hai, and please woh pricing deck le aaye."),
    ("assistant", "Puck", "Got it: the meeting moved to Thursday at four pm, and {owner} should bring the pricing deck. I'll make sure {owner} confirms with you. Anything else?"),
    ("caller", "Kore", "Nahi bas itna hi. Thank you!"),
    ("assistant", "Puck", "Thanks Priya, have a good day. Bye!"),
]


async def tts(client: genai.Client, text: str, voice: str) -> np.ndarray:
    resp = await client.aio.models.generate_content(
        model=TTS_MODEL,
        contents=f"Say naturally, like on a phone call: {text}",
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))
            ),
        ),
    )
    data = resp.candidates[0].content.parts[0].inline_data.data  # type: ignore[index,union-attr]
    return np.frombuffer(data, dtype="<i2")


async def main() -> None:
    s = get_settings()
    await db.open_pool()
    await migrate()
    client = genai.Client(api_key=s.gemini_api_key)
    rate = s.record_sample_rate
    lines = [(spk, text.format(owner=s.owner_name)) for spk, _, text in SCRIPT]
    print("Synthesizing speech…")
    clips = await asyncio.gather(*(tts(client, text.format(owner=s.owner_name), voice) for _, voice, text in SCRIPT))

    call_id = await repo.create_call(direction="incoming", mode="test", contact_name="Priya", contact_phone="",
                                     purpose="", voice="Puck")
    wav = s.recordings_dir / f"call_{call_id}_{datetime.now():%Y%m%d_%H%M%S}_sample.wav"
    rec = StereoRecorder(wav, rate)
    rec.start()
    t, pos_ms, block = 1000.0, 0, rate // 50
    for seq, ((spk, text), clip) in enumerate(zip(lines, clips)):
        pcm = resample(clip, 24000, rate)
        pcm = np.concatenate([pcm, np.zeros(int(rate * 0.6), np.int16)])  # pause between turns
        silence = np.zeros_like(pcm)
        left, right = (pcm, silence) if spk == "caller" else (silence, pcm)
        for i in range(0, pcm.size, block):
            t += block / rate
            rec.write_caller(left[i:i + block], t)
            rec.write_ai(right[i:i + block], t)
        dur_ms = int(pcm.size * 1000 / rate)
        # Simulate a slightly imperfect live transcript (as real-time ASR would produce)
        live_text = text.replace("pricing deck", "pricing tech") if spk == "caller" else text
        await repo.insert_segment(call_id, seq, spk, live_text, pos_ms, pos_ms + dur_ms, False)
        pos_ms += dur_ms
    path = await asyncio.to_thread(rec.finalize)
    await repo.finish_call(call_id, end_reason="sample", recording_path=str(path),
                           transcript_text="\n".join(f"{a.capitalize()}: {b}" for a, b in lines))
    print(f"Sample call #{call_id} created ({pos_ms / 1000:.1f}s audio): {path}")
    print("Queued for processing. Run the worker (python -m app.worker) or python run.py.")
    await db.close_pool()


if __name__ == "__main__":
    db.use_selector_loop_on_windows()
    asyncio.run(main())
