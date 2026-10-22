"""All SQL lives here. Parameterized queries only."""
from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from .config import get_settings
from .db import pool

# Jobs are tagged with the machine that holds the recording file, so a worker
# on another machine sharing this database never grabs a job it cannot process.
HOST = socket.gethostname()

DEFAULT_STYLE_RULES = """\
STYLE
- I talk casually and warmly, short sentences. I say "haan", "achha", "theek hai" with Hindi speakers.
- I get to the point quickly and don't over-explain.

RULES (what my assistant MAY agree to)
- Taking messages and noting callback requests: always OK.
- Scheduling: can say I'm usually free after 6 pm on weekdays, but must say I'll confirm the exact time.
- Never agree to payments, loans, purchases, sharing OTPs/bank details, or meetings on my behalf.
- For anything not covered here: say "Let me check and get back to you."
"""


# ---------------------------------------------------------------- settings
async def get_app_settings() -> dict[str, Any]:
    s = get_settings()
    out: dict[str, Any] = {
        "style_rules": DEFAULT_STYLE_RULES,
        "voice": s.default_voice,
        "recording_enabled": s.default_recording_enabled,
        "silence_timeout_s": s.default_silence_timeout_s,
    }
    async with pool().connection() as conn:
        for r in await (await conn.execute("SELECT key, value FROM app_settings")).fetchall():
            out[r["key"]] = r["value"]
    return out


async def update_app_settings(values: dict[str, Any]) -> None:
    async with pool().connection() as conn, conn.transaction():
        for k, v in values.items():
            await conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                (k, Jsonb(v)),
            )


# ---------------------------------------------------------------- calls
async def create_call(*, direction: str, mode: str, contact_name: str, contact_phone: str, purpose: str, voice: str,
                      provider_call_id: str = "") -> int:
    async with pool().connection() as conn:
        row = await (await conn.execute(
            "INSERT INTO calls (direction, mode, contact_name, contact_phone, purpose, voice, provider_call_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (direction, mode, contact_name, contact_phone, purpose, voice, provider_call_id),
        )).fetchone()
    assert row
    return int(row["id"])


async def finish_call(call_id: int, *, end_reason: str, recording_path: str | None, transcript_text: str, error: str = "") -> None:
    """Close a live call and enqueue post-call processing atomically."""
    async with pool().connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE calls SET status='queued', ended_at=now(), "
            "duration_s=GREATEST(0, EXTRACT(EPOCH FROM now()-started_at))::int, "
            "end_reason=%s, recording_path=%s, transcript_text=%s, error=%s WHERE id=%s",
            (end_reason, recording_path, transcript_text, error, call_id),
        )
        await conn.execute(
            "INSERT INTO jobs (kind, payload) VALUES ('process_call', %s)", (Jsonb({"call_id": call_id, "host": HOST}),)
        )


async def set_recording_path(call_id: int, path: str) -> None:
    async with pool().connection() as conn:
        await conn.execute("UPDATE calls SET recording_path=%s WHERE id=%s", (path, call_id))


async def insert_segment(call_id: int, seq: int, speaker: str, text: str, start_ms: int, end_ms: int, interrupted: bool) -> None:
    async with pool().connection() as conn:
        await conn.execute(
            "INSERT INTO transcript_segments (call_id, source, seq, speaker, text, start_ms, end_ms, interrupted) "
            "VALUES (%s, 'live', %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (call_id, source, seq) DO UPDATE SET text=EXCLUDED.text, end_ms=EXCLUDED.end_ms, "
            "interrupted=EXCLUDED.interrupted",
            (call_id, seq, speaker, text, start_ms, end_ms, interrupted),
        )


_CALL_LIST_COLS = (
    "id, direction, mode, contact_name, contact_phone, purpose, status, started_at, ended_at, duration_s, "
    "title, sentiment, left(summary, 220) AS summary_preview, error"
)


async def list_calls(q: str = "", limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    async with pool().connection() as conn:
        if q.strip():
            rows = await (await conn.execute(
                f"SELECT {_CALL_LIST_COLS}, ts_rank(search, query) AS rank, "
                "ts_headline('simple', coalesce(summary,'') || ' ' || transcript_text, query, "
                "'MaxWords=24, MinWords=8, MaxFragments=1, StartSel=<mark>, StopSel=</mark>') AS snippet "
                "FROM calls, websearch_to_tsquery('simple', %s) query "
                "WHERE search @@ query ORDER BY rank DESC, started_at DESC LIMIT %s OFFSET %s",
                (q, limit, offset),
            )).fetchall()
        else:
            rows = await (await conn.execute(
                f"SELECT {_CALL_LIST_COLS} FROM calls ORDER BY started_at DESC LIMIT %s OFFSET %s", (limit, offset)
            )).fetchall()
    return rows


async def get_call(call_id: int) -> dict[str, Any] | None:
    async with pool().connection() as conn:
        call = await (await conn.execute("SELECT * FROM calls WHERE id=%s", (call_id,))).fetchone()
        if not call:
            return None
        call.pop("search", None)
        segs = await (await conn.execute(
            "SELECT source, seq, speaker, text, start_ms, end_ms, interrupted FROM transcript_segments "
            "WHERE call_id=%s ORDER BY source, start_ms, seq", (call_id,)
        )).fetchall()
        items = await (await conn.execute(
            "SELECT id, owner, task, due, done FROM action_items WHERE call_id=%s ORDER BY id", (call_id,)
        )).fetchall()
    call["live_transcript"] = [s for s in segs if s["source"] == "live"]
    call["corrected_transcript"] = [s for s in segs if s["source"] == "corrected"]
    call["action_items"] = items
    return call


async def delete_call(call_id: int) -> str | None:
    async with pool().connection() as conn:
        row = await (await conn.execute("DELETE FROM calls WHERE id=%s RETURNING recording_path", (call_id,))).fetchone()
    return row["recording_path"] if row else None


async def recent_summaries(contact_name: str, contact_phone: str = "", limit: int = 3,
                           exclude_id: int | None = None) -> list[dict[str, Any]]:
    """Last calls with this person, matched by phone number or (case-insensitive) name."""
    name, phone = contact_name.strip(), contact_phone.strip()
    if not name and not phone:
        return []
    async with pool().connection() as conn:
        return await (await conn.execute(
            "SELECT id, started_at, direction, title, summary FROM calls "
            "WHERE ((%s <> '' AND contact_phone = %s) OR (%s <> '' AND lower(contact_name) = lower(%s))) "
            "AND summary <> '' AND id <> %s ORDER BY started_at DESC LIMIT %s",
            (phone, phone, name, name, exclude_id or -1, limit),
        )).fetchall()


async def recover_stuck_calls() -> list[dict[str, Any]]:
    """Calls left 'live' by a crash: return them so the caller can finalize audio and enqueue processing."""
    async with pool().connection() as conn:
        return await (await conn.execute("SELECT id, recording_path FROM calls WHERE status='live'")).fetchall()


async def live_transcript_text(call_id: int) -> str:
    async with pool().connection() as conn:
        rows = await (await conn.execute(
            "SELECT speaker, text FROM transcript_segments WHERE call_id=%s AND source='live' ORDER BY start_ms, seq", (call_id,)
        )).fetchall()
    return "\n".join(f"{r['speaker'].capitalize()}: {r['text']}" for r in rows)


async def set_call_status(call_id: int, status: str, error: str = "") -> None:
    async with pool().connection() as conn:
        await conn.execute("UPDATE calls SET status=%s, error=%s WHERE id=%s", (status, error, call_id))


@dataclass
class CorrectedSegment:
    speaker: str
    text: str


async def save_analysis(
    call_id: int,
    *,
    title: str,
    summary: str,
    sentiment: str,
    analysis: dict[str, Any],
    corrected: list[CorrectedSegment],
    action_items: list[dict[str, str]],
    facts: list[tuple[str, str, str]],
    caller_name: str = "",
) -> None:
    transcript_text = "\n".join(f"{c.speaker.capitalize()}: {c.text}" for c in corrected)
    async with pool().connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE calls SET status='done', error='', title=%s, summary=%s, sentiment=%s, analysis=%s, "
            "transcript_text = CASE WHEN %s <> '' THEN %s ELSE transcript_text END WHERE id=%s",
            (title, summary, sentiment, Jsonb(analysis), transcript_text, transcript_text, call_id),
        )
        await conn.execute("DELETE FROM transcript_segments WHERE call_id=%s AND source='corrected'", (call_id,))
        for i, c in enumerate(corrected):
            await conn.execute(
                "INSERT INTO transcript_segments (call_id, source, seq, speaker, text) VALUES (%s,'corrected',%s,%s,%s)",
                (call_id, i, c.speaker, c.text),
            )
        await conn.execute("DELETE FROM action_items WHERE call_id=%s", (call_id,))
        for a in action_items:
            await conn.execute(
                "INSERT INTO action_items (call_id, owner, task, due) VALUES (%s,%s,%s,%s)",
                (call_id, a.get("owner", ""), a["task"], a.get("due", "")),
            )
        for fact, contact, phone in facts:
            await conn.execute(
                "INSERT INTO memory_facts (fact, contact_name, contact_phone, source_call_id) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (lower(contact_name), contact_phone, lower(fact)) DO NOTHING",
                (fact, contact, phone, call_id),
            )
        if caller_name:
            # Unknown caller said their name: remember it for next time (never overrides a name you set).
            row = await (await conn.execute(
                "SELECT contact_phone, contact_name FROM calls WHERE id=%s", (call_id,))).fetchone()
            if row and row["contact_phone"] and not row["contact_name"]:
                await conn.execute(
                    "INSERT INTO contacts (phone, name, source) VALUES (%s,%s,'auto') ON CONFLICT (phone) DO NOTHING",
                    (row["contact_phone"], caller_name[:200]),
                )
                await conn.execute("UPDATE calls SET contact_name=%s WHERE id=%s", (caller_name[:200], call_id))


# ---------------------------------------------------------------- memory
async def list_facts(contact_name: str | None = None, contact_phone: str = "") -> list[dict[str, Any]]:
    """All facts (contact_name=None), or the facts relevant to one call: general + about this person."""
    async with pool().connection() as conn:
        if contact_name is None:
            return await (await conn.execute(
                "SELECT id, fact, contact_name, contact_phone, source_call_id, created_at FROM memory_facts "
                "ORDER BY created_at DESC"
            )).fetchall()
        name, phone = contact_name.strip(), contact_phone.strip()
        return await (await conn.execute(
            "SELECT id, fact, contact_name, contact_phone FROM memory_facts "
            "WHERE (contact_name = '' AND contact_phone = '') "
            "   OR (%s <> '' AND lower(contact_name) = lower(%s)) OR (%s <> '' AND contact_phone = %s) "
            "ORDER BY (contact_name = '' AND contact_phone = ''), created_at DESC LIMIT 80",
            (name, name, phone, phone),
        )).fetchall()


async def add_fact(fact: str, contact_name: str = "", contact_phone: str = "") -> dict[str, Any] | None:
    async with pool().connection() as conn:
        return await (await conn.execute(
            "INSERT INTO memory_facts (fact, contact_name, contact_phone) VALUES (%s,%s,%s) "
            "ON CONFLICT (lower(contact_name), contact_phone, lower(fact)) DO NOTHING "
            "RETURNING id, fact, contact_name, contact_phone, created_at",
            (fact, contact_name, contact_phone),
        )).fetchone()


async def delete_fact(fact_id: int) -> bool:
    async with pool().connection() as conn:
        cur = await conn.execute("DELETE FROM memory_facts WHERE id=%s", (fact_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------- action items
async def list_open_actions() -> list[dict[str, Any]]:
    async with pool().connection() as conn:
        return await (await conn.execute(
            "SELECT a.id, a.call_id, a.owner, a.task, a.due, a.done, c.contact_name, c.started_at "
            "FROM action_items a JOIN calls c ON c.id=a.call_id WHERE NOT a.done ORDER BY a.created_at DESC LIMIT 100"
        )).fetchall()


async def set_action_done(item_id: int, done: bool) -> bool:
    async with pool().connection() as conn:
        cur = await conn.execute("UPDATE action_items SET done=%s WHERE id=%s", (done, item_id))
        return cur.rowcount > 0


# ---------------------------------------------------------------- analytics
async def analytics(tz: str) -> dict[str, Any]:
    async with pool().connection() as conn:
        totals = await (await conn.execute(
            "SELECT count(*) AS total_calls, coalesce(sum(duration_s),0) AS total_seconds, "
            "coalesce(avg(duration_s),0) AS avg_seconds FROM calls WHERE status <> 'live'"
        )).fetchone()
        open_items = await (await conn.execute("SELECT count(*) AS n FROM action_items WHERE NOT done")).fetchone()
        per_day = await (await conn.execute(
            "SELECT d::date AS day, count(c.id) AS calls, coalesce(sum(c.duration_s),0) AS seconds "
            "FROM generate_series((now() AT TIME ZONE %s)::date - 13, (now() AT TIME ZONE %s)::date, '1 day') d "
            "LEFT JOIN calls c ON (c.started_at AT TIME ZONE %s)::date = d::date "
            "GROUP BY d ORDER BY d",
            (tz, tz, tz),
        )).fetchall()
        by_dir = await (await conn.execute("SELECT direction, count(*) AS n FROM calls GROUP BY direction")).fetchall()
    assert totals and open_items
    return {
        "total_calls": int(totals["total_calls"]),
        "total_minutes": round(float(totals["total_seconds"]) / 60, 1),
        "avg_seconds": round(float(totals["avg_seconds"])),
        "open_action_items": int(open_items["n"]),
        "per_day": [{"day": r["day"].isoformat(), "calls": int(r["calls"]), "minutes": round(int(r["seconds"]) / 60, 1)} for r in per_day],
        "by_direction": {r["direction"]: int(r["n"]) for r in by_dir},
    }


# ---------------------------------------------------------------- job queue
async def enqueue_job(kind: str, payload: dict[str, Any]) -> int:
    async with pool().connection() as conn:
        row = await (await conn.execute(
            "INSERT INTO jobs (kind, payload) VALUES (%s,%s) RETURNING id", (kind, Jsonb(payload))
        )).fetchone()
    assert row
    return int(row["id"])


async def claim_job() -> dict[str, Any] | None:
    """Atomically claim one ready job. Also reclaims jobs whose worker died (locked > 15 min)."""
    async with pool().connection() as conn:
        return await (await conn.execute(
            """
            UPDATE jobs SET status='running', attempts=attempts+1, locked_at=now(), updated_at=now()
            WHERE id = (
                SELECT id FROM jobs
                WHERE ((status='pending' AND run_after <= now())
                       OR (status='running' AND locked_at < now() - interval '15 minutes'))
                  AND coalesce(payload->>'host', %s) = %s
                ORDER BY run_after FOR UPDATE SKIP LOCKED LIMIT 1
            )
            RETURNING id, kind, payload, attempts, max_attempts
            """,
            (HOST, HOST),
        )).fetchone()


async def complete_job(job_id: int) -> None:
    async with pool().connection() as conn:
        await conn.execute("UPDATE jobs SET status='done', locked_at=NULL, updated_at=now() WHERE id=%s", (job_id,))


async def fail_job(job_id: int, error: str, retry: bool, backoff_s: int) -> None:
    async with pool().connection() as conn:
        await conn.execute(
            "UPDATE jobs SET status=%s, last_error=%s, locked_at=NULL, updated_at=now(), "
            "run_after = now() + make_interval(secs => %s) WHERE id=%s",
            ("pending" if retry else "failed", error[:4000], backoff_s, job_id),
        )


async def requeue_call(call_id: int) -> None:
    async with pool().connection() as conn, conn.transaction():
        await conn.execute("UPDATE calls SET status='queued', error='' WHERE id=%s", (call_id,))
        await conn.execute("INSERT INTO jobs (kind, payload) VALUES ('process_call', %s)",
                           (Jsonb({"call_id": call_id, "host": HOST}),))


# ---------------------------------------------------------------- contacts
async def contact_name_for(phone: str) -> str:
    if not phone:
        return ""
    async with pool().connection() as conn:
        row = await (await conn.execute("SELECT name FROM contacts WHERE phone=%s", (phone,))).fetchone()
    return row["name"] if row else ""


async def list_contacts() -> list[dict[str, Any]]:
    async with pool().connection() as conn:
        return await (await conn.execute(
            "SELECT c.phone, c.name, c.source, c.updated_at, "
            "(SELECT count(*) FROM calls WHERE contact_phone=c.phone) AS calls "
            "FROM contacts c ORDER BY lower(c.name)"
        )).fetchall()


async def upsert_contact(phone: str, name: str) -> None:
    """Manual save: also relabels past calls from this number."""
    async with pool().connection() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO contacts (phone, name, source) VALUES (%s,%s,'manual') "
            "ON CONFLICT (phone) DO UPDATE SET name=EXCLUDED.name, source='manual', updated_at=now()",
            (phone, name),
        )
        await conn.execute("UPDATE calls SET contact_name=%s WHERE contact_phone=%s", (name, phone))


async def delete_contact(phone: str) -> bool:
    async with pool().connection() as conn:
        cur = await conn.execute("DELETE FROM contacts WHERE phone=%s", (phone,))
        return cur.rowcount > 0


async def set_hangup_cause(provider_call_id: str, cause: str) -> None:
    async with pool().connection() as conn:
        await conn.execute("UPDATE calls SET hangup_cause=%s WHERE provider_call_id=%s", (cause[:200], provider_call_id))
