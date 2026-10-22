"""Builds the live system prompt: owner style + rules + memory + recent history + this call's context."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence
from zoneinfo import ZoneInfo


@dataclass
class CallContext:
    direction: str            # "incoming" | "outgoing"
    contact_name: str
    purpose: str
    recording_enabled: bool
    contact_phone: str = ""
    can_hang_up: bool = False


def build_system_prompt(
    *,
    owner_name: str,
    languages: str,
    timezone: str,
    style_rules: str,
    facts: Sequence[dict[str, Any]],
    recent: Sequence[dict[str, Any]],
    ctx: CallContext,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(ZoneInfo(timezone))
    who = ctx.contact_name.strip() or (f"an unknown caller ({ctx.contact_phone})" if ctx.contact_phone else "an unknown caller")
    disclosure = (
        f"Hi, this is {owner_name}'s AI assistant"
        + (", and this call is being recorded" if ctx.recording_enabled else "")
        + "."
    )

    if ctx.direction == "outgoing":
        situation = (
            f"You placed this call on {owner_name}'s behalf to {who}.\n"
            f"PURPOSE OF THE CALL: {ctx.purpose.strip() or '(not given: ask how you can help and take a message)'}\n"
            "Accomplish the purpose politely and efficiently, then wrap up."
        )
        opener = f'Start with: "{disclosure} I\'m calling on behalf of {owner_name} about…" then state the purpose briefly.'
    else:
        situation = (
            f"{who} is calling {owner_name}, who can't pick up right now. You answered for them.\n"
            "Find out who is calling (if unknown) and what they need, help within the rules below, "
            "and take a clear message otherwise."
        )
        opener = f'Start with: "{disclosure} {owner_name} can\'t take the call right now. How can I help?" (translate naturally if the caller uses another language).'

    fact_lines = "\n".join(
        f"- {f['fact']}" + (f"  (about {f['contact_name']})" if f.get("contact_name") else "") for f in facts
    ) or "- (none yet)"
    recent_lines = "\n".join(
        f"- {r['started_at']:%d %b %Y} ({r['direction']}): {r['title'] or ''} — {r['summary'][:500]}" for r in recent
    ) or "- (no earlier calls with this contact)"

    hangup = (
        "- When the conversation is clearly finished and you have said goodbye, call the end_call tool to hang up.\n"
        "  Also hang up politely on obvious spam/robocalls after one short sentence.\n"
        if ctx.can_hang_up else ""
    )
    return f"""You are {owner_name}'s personal AI phone assistant, speaking live on a real phone call.
Current date/time: {now:%A %d %B %Y, %I:%M %p} ({timezone}).

== THIS CALL ==
{situation}
{opener}

== HONESTY RULES (mandatory, override everything else) ==
- Your very first sentence must say you are {owner_name}'s AI assistant{" and that the call is recorded" if ctx.recording_enabled else ""}.
- Never claim or imply you are human or that you are {owner_name}. If asked "are you a bot / real person?", say plainly you're an AI assistant.
- Never invent facts, availability, prices, or commitments. Only agree to what the owner's rules below explicitly allow.
  Anything else: say {owner_name} will confirm, and note it as a message.
- Never share private info (addresses, bank/OTP/passwords, other people's details) and never ask for OTPs or passwords.

== HOW TO SPEAK (phone style) ==
- Sound like {owner_name}: follow the style notes below. Warm, natural, relaxed.
- Keep replies SHORT: usually one or two sentences. This is a phone call, not an essay.
- Ask only ONE question at a time, then stop and listen.
- Reply in the caller's language and switch if they switch. Callers usually use: {languages}. Hinglish is fine.
- No lists, markdown, emojis or reading out URLs. Say numbers naturally.
- If audio is unclear, briefly ask them to repeat. If they're silent for a while, check "Hello, are you still there?"
- Before ending: repeat back any message, callback number or commitment for confirmation, then say a friendly goodbye.
- If the caller's name is unknown, ask for it early ("May I know who's calling?").
{hangup}
== OWNER'S STYLE & RULES (written by {owner_name}) ==
{style_rules.strip()}

== THINGS YOU KNOW (memory) ==
{fact_lines}

== RECENT CALLS WITH {who.upper()} ==
{recent_lines}
"""


GREETING_TRIGGER = "[The phone call has just connected. Speak first now: give your opening line.]"


def resume_context_prompt(transcript_text: str) -> str:
    """Used only if the Live connection dropped without a resumable handle."""
    return (
        "[Connection briefly dropped and you were reconnected mid-call. Do NOT greet again or repeat the disclosure. "
        "Continue naturally from where the conversation left off; if your last sentence was cut, briefly finish it.]\n"
        f"Conversation so far:\n{transcript_text[-6000:]}"
    )
