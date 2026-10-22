"""Post-call analysis: recording + live transcript -> structured JSON via Gemini."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from . import repo
from .config import get_settings

log = logging.getLogger(__name__)


class PromiseMade(BaseModel):
    by: str = Field(description="Who made the promise: the caller's name, 'Assistant', or the owner's name.")
    to: str = Field(description="Who the promise was made to.")
    what: str = Field(description="What exactly was promised.")
    due: str = Field("", description="When, exactly as said (e.g. 'by Friday'), or empty.")


class ActionItem(BaseModel):
    owner: str = Field(description="Who must do it.")
    task: str = Field(description="The concrete task.")
    due: str = Field("", description="Due date/time exactly as said, or empty.")


class Utterance(BaseModel):
    speaker: Literal["caller", "assistant"]
    text: str = Field(description="What was said, in the original language and script.")


class MemoryFact(BaseModel):
    fact: str = Field(description="A durable fact worth remembering for future calls, one sentence.")
    about: str = Field("", description="Contact name the fact is about, or empty if it's about the owner in general.")


class CallAnalysis(BaseModel):
    caller_name: str = Field("", description="The caller's/other party's name ONLY if they clearly said it, else empty.")
    title: str = Field(description="Short title, max 8 words.")
    summary: str = Field(description="2-4 sentence factual summary.")
    key_points: list[str]
    decisions: list[str]
    promises: list[PromiseMade]
    action_items: list[ActionItem]
    follow_ups: list[str] = Field(description="Things the owner should follow up on personally.")
    sentiment: Literal["positive", "neutral", "negative", "mixed"]
    language: str = Field(description="Main language(s) spoken, e.g. 'Hinglish'.")
    transcript: list[Utterance] = Field(description="Corrected, speaker-labelled transcript of the whole call, in order.")
    memory_facts: list[MemoryFact]


def analysis_prompt(call: dict, live_transcript: str, owner: str, has_audio: bool) -> str:
    return f"""You are analysing a phone call handled by {owner}'s AI phone assistant.
Call direction: {call['direction']}. Contact: {call['contact_name'] or 'unknown'} {call.get('contact_phone') or ''}. Purpose given: {call['purpose'] or '-'}.
{"The attached audio is STEREO: LEFT channel = the caller, RIGHT channel = the AI assistant." if has_audio else "No audio is available; use the live transcript only."}

Rules:
- Only report what was actually said. Never invent names, numbers, dates, promises or tasks. Empty lists are fine.
- Keep the original language and script of what was said (Hindi stays Hindi, Hinglish stays Hinglish). Write the summary
  and other fields in the language the caller mainly used, but use English if the call was mainly English.
- "transcript": produce a corrected, complete, speaker-labelled transcript from the audio, using the live transcript as a hint
  (it may have recognition errors or missing words). Merge fragments into natural turns.
- "promises": anything someone committed to (including the assistant saying {owner} will call back).
- "action_items": concrete tasks; the owner is usually {owner} unless the caller took the task.
- "memory_facts": only durable, useful facts about the caller or {owner} (relationship, preferences, recurring needs,
  important dates). Not one-off details of this call. Max 5. Avoid duplicating obvious facts.

Live transcript (machine-generated during the call):
{live_transcript or '(empty)'}
"""


def _fact_target(about: str, contact: str, phone: str, caller_name: str) -> tuple[str, str]:
    """Map the model's 'about' to (contact_name, contact_phone) used for lookups, so the fact is found next call."""
    about = (about or "").strip()
    if not about:
        return "", ""  # general fact about the owner
    for n in (contact.strip(), caller_name.strip()):
        if n and (about.lower() in n.lower() or n.lower() in about.lower()):
            return contact.strip() or caller_name.strip(), phone
    return about[:200], ""


async def _wait_active(client: genai.Client, f: types.File, timeout_s: float = 120) -> types.File:
    waited = 0.0
    while f.state and f.state.name == "PROCESSING":
        if waited >= timeout_s:
            raise TimeoutError("Gemini file processing timed out")
        await asyncio.sleep(1.5)
        waited += 1.5
        f = await client.aio.files.get(name=f.name)  # type: ignore[arg-type]
    if f.state and f.state.name == "FAILED":
        raise RuntimeError("Gemini could not process the audio file")
    return f


async def process_call(call_id: int) -> None:
    s = get_settings()
    call = await repo.get_call(call_id)
    if call is None:
        log.info("call %s no longer exists; skipping", call_id)
        return
    await repo.set_call_status(call_id, "processing")
    live_tx = "\n".join(f"{seg['speaker'].capitalize()}: {seg['text']}" for seg in call["live_transcript"])
    rec = Path(call["recording_path"]) if call.get("recording_path") else None
    has_audio = bool(rec and rec.exists() and rec.stat().st_size > 16000)  # > ~0.25 s of audio

    if not has_audio and not live_tx.strip():
        await repo.save_analysis(call_id, title="No conversation", summary="Nothing was said on this call.",
                                 sentiment="neutral", analysis={}, corrected=[], action_items=[], facts=[])
        return

    client = genai.Client(api_key=s.gemini_api_key)
    uploaded: types.File | None = None
    try:
        contents: list = [analysis_prompt(call, live_tx, s.owner_name, has_audio)]
        if has_audio:
            uploaded = await client.aio.files.upload(
                file=str(rec), config=types.UploadFileConfig(mime_type="audio/wav", display_name=f"call-{call_id}")
            )
            uploaded = await _wait_active(client, uploaded)
            contents.append(uploaded)
        resp = await client.aio.models.generate_content(
            model=s.postcall_model,
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=CallAnalysis.model_json_schema(),
                temperature=0.2,
            ),
        )
        if not resp.text:
            raise RuntimeError(f"Empty response from Gemini (finish reason: {resp.candidates[0].finish_reason if resp.candidates else 'unknown'})")
        a = CallAnalysis.model_validate_json(resp.text)
        contact = call["contact_name"]
        await repo.save_analysis(
            call_id,
            title=a.title.strip()[:200],
            summary=a.summary.strip(),
            sentiment=a.sentiment,
            analysis=a.model_dump(exclude={"transcript"}),
            corrected=[repo.CorrectedSegment(u.speaker, u.text.strip()) for u in a.transcript if u.text.strip()],
            action_items=[i.model_dump() for i in a.action_items if i.task.strip()],
            facts=[(f.fact.strip()[:1000], *_fact_target(f.about, contact, call["contact_phone"], a.caller_name))
                   for f in a.memory_facts[:5] if f.fact.strip()],
            caller_name=a.caller_name.strip(),
        )
        log.info("call %s analysed: %s", call_id, a.title)
    finally:
        if uploaded is not None and uploaded.name:
            try:
                await client.aio.files.delete(name=uploaded.name)
            except Exception as e:  # files auto-expire after 48 h anyway
                log.warning("could not delete uploaded file %s: %s", uploaded.name, e)
