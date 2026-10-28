"""Background worker: processes the Postgres job queue. Run: python -m app.worker"""
from __future__ import annotations

import asyncio
import logging
import signal
import time

from . import db, repo
from .migrate import migrate
from .postcall import process_call

log = logging.getLogger("worker")
POLL_S = 2.0
ACTIVE_WINDOW_S = 180   # keep polling the DB this long after any activity (covers the 15 s / 45 s retries)
IDLE_POLL_S = 3600      # otherwise only on a wake signal, plus this fallback, so a serverless DB (Neon) can sleep


def _wake_mtime() -> float:
    try:
        return repo.wake_file().stat().st_mtime
    except OSError:
        return 0.0


def _is_fatal(e: Exception) -> bool:
    m = str(e).lower()
    return "api key" in m or "permission" in m or ("not found" in m and "model" in m)


async def handle(job: dict) -> None:
    if job["kind"] == "process_call":
        await process_call(int(job["payload"]["call_id"]))
    else:
        raise ValueError(f"unknown job kind {job['kind']!r}")


async def run_once() -> bool:
    """Process one job if available. Returns True if a job was handled."""
    job = await repo.claim_job()
    if not job:
        return False
    log.info("job %s (%s) attempt %d/%d", job["id"], job["kind"], job["attempts"], job["max_attempts"])
    try:
        await handle(job)
        await repo.complete_job(job["id"])
        log.info("job %s done", job["id"])
    except Exception as e:
        log.exception("job %s failed", job["id"])
        retry = job["attempts"] < job["max_attempts"] and not _is_fatal(e)
        backoff = 15 * 3 ** (job["attempts"] - 1)  # 15 s, 45 s
        await repo.fail_job(job["id"], f"{type(e).__name__}: {e}", retry, backoff)
        call_id = job["payload"].get("call_id")
        if call_id:
            msg = f"Summary attempt {job['attempts']} failed: {e}"
            await repo.set_call_status(int(call_id), "queued" if retry else "failed",
                                       msg + (f" (retrying in {backoff}s)" if retry else ""))
    return True


async def main() -> None:
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):
            pass
    try:
        await db.open_pool(max_size=3)
    except Exception as e:
        raise SystemExit(db.describe_db_error(e)) from None
    await migrate()
    log.info("worker started; waiting for jobs")
    last_active = last_db_poll = time.monotonic()
    seen_wake = _wake_mtime()
    try:
        while not stop.is_set():
            now = time.monotonic()
            wake = _wake_mtime()
            if wake != seen_wake:
                seen_wake, last_active = wake, now
            if now - last_active < ACTIVE_WINDOW_S or now - last_db_poll >= IDLE_POLL_S:
                last_db_poll = now
                try:
                    if await run_once():
                        last_active = time.monotonic()
                        continue
                except Exception:
                    log.exception("worker loop error (database hiccup?); continuing")
            try:
                await asyncio.wait_for(stop.wait(), timeout=POLL_S)
            except asyncio.TimeoutError:
                pass
    finally:
        await db.close_pool()
        log.info("worker stopped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.use_selector_loop_on_windows()
    asyncio.run(main())
