"""Async Postgres pool (psycopg 3); works with the Supabase pooler and Neon."""
from __future__ import annotations

import asyncio
import logging
import sys
from urllib.parse import parse_qs, urlparse

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import get_settings

log = logging.getLogger(__name__)
_pool: AsyncConnectionPool | None = None


def use_selector_loop_on_windows() -> None:
    """psycopg async cannot run on Windows' default Proactor loop."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]


def _conninfo() -> str:
    url = get_settings().database_url.strip()
    if "sslmode" not in parse_qs(urlparse(url).query):
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


async def _configure(conn: AsyncConnection) -> None:
    conn.prepare_threshold = None  # never use server-side prepared statements (pooler-safe)


def describe_db_error(e: BaseException) -> str:
    msg = str(e)
    if "password authentication failed" in msg:
        return "Database login failed: check the password in DATABASE_URL (percent-encode special characters like @ # ?)."
    if "Tenant or user not found" in msg:
        return "Supabase says 'Tenant or user not found': username must be postgres.<project-ref> and host must match your region."
    if "getaddrinfo" in msg or "could not translate host name" in msg:
        return "Database host not reachable: check internet / DATABASE_URL host. Use the Session pooler string (IPv4)."
    if "timeout" in msg.lower():
        return "Database connection timed out: check your network or firewall, and that you're using port 5432 (session pooler)."
    return f"Database error: {msg}"


async def open_pool(min_size: int = 0, max_size: int = 6) -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            _conninfo(),
            min_size=min_size,
            max_size=max_size,
            open=False,
            configure=_configure,
            kwargs={"row_factory": dict_row, "autocommit": True, "prepare_threshold": None},
            max_idle=120,  # with min_size=0 idle connections close, so a serverless DB (Neon) can scale to zero
            reconnect_timeout=60,
        )
        await _pool.open(wait=True, timeout=20)
        log.info("database pool open")
    return _pool


def pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("database pool not open")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
