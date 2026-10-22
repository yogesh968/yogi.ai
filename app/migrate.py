"""Apply SQL migrations in app/migrations in order. Run: python -m app.migrate"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from . import db

MIGRATIONS = Path(__file__).parent / "migrations"
log = logging.getLogger(__name__)


async def migrate() -> list[str]:
    applied: list[str] = []
    async with db.pool().connection() as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r["name"] for r in await (await conn.execute("SELECT name FROM schema_migrations")).fetchall()}
        for f in sorted(MIGRATIONS.glob("*.sql")):
            if f.name in done:
                continue
            async with conn.transaction():
                # Advisory lock so server + worker starting together don't race.
                await conn.execute("SELECT pg_advisory_xact_lock(424242)")
                if await (await conn.execute("SELECT 1 FROM schema_migrations WHERE name=%s", (f.name,))).fetchone():
                    continue
                await conn.execute(f.read_text(encoding="utf-8"))  # type: ignore[arg-type]  # trusted local file
                await conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
            applied.append(f.name)
            log.info("applied migration %s", f.name)
    return applied


async def _main() -> None:
    try:
        await db.open_pool(max_size=1)
    except Exception as e:
        raise SystemExit(db.describe_db_error(e)) from None
    try:
        applied = await migrate()
        print("Applied:", ", ".join(applied) if applied else "nothing (schema up to date)")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    db.use_selector_loop_on_windows()
    asyncio.run(_main())
