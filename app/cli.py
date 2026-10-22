"""Terminal test mode: talk to the assistant with your normal mic + headphones.

    python -m app.cli talk                       # incoming call simulation
    python -m app.cli talk --direction outgoing --name "Ravi" --purpose "Confirm Saturday meeting at 5"
    python -m app.cli check                      # verify .env, DB, devices and Gemini key

Press Ctrl+C to hang up.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from . import db
from .agent import CallAgent, CallOptions
from .audio.devices import AudioDeviceError
from .config import get_settings
from .events import bus

C = {"caller": "\033[96m", "assistant": "\033[95m", "dim": "\033[90m", "red": "\033[91m", "end": "\033[0m"}


async def _printer() -> None:
    q = bus.subscribe()
    while True:
        ev = await q.get()
        t = ev.get("type")
        if t == "segment" and ev.get("final"):
            who = "Caller   " if ev["speaker"] == "caller" else "Assistant"
            mark = " …(interrupted)" if ev.get("interrupted") else ""
            print(f"{C[ev['speaker']]}{who}{C['end']} {ev['text']}{C['dim']}{mark}{C['end']}")
        elif t == "status":
            col = C["red"] if ev["state"] == "error" else C["dim"]
            print(f"{col}[{ev['state']}] {ev.get('message', '')}{C['end']}")
        elif t == "latency":
            print(f"{C['dim']}  ⏱ response latency {ev['ms']} ms{C['end']}")
        elif t == "devices":
            print(f"{C['dim']}  🎙 {ev['input']}\n  🔊 {ev['output']}{C['end']}")


async def talk(args: argparse.Namespace) -> None:
    try:
        await db.open_pool()
    except Exception as e:
        raise SystemExit(db.describe_db_error(e)) from None
    from .migrate import migrate
    await migrate()
    bus.bind_loop(asyncio.get_running_loop())
    printer = asyncio.create_task(_printer())
    agent = CallAgent(CallOptions(mode=args.mode, direction=args.direction, contact_name=args.name,
                                  contact_phone=args.phone, purpose=args.purpose))
    try:
        signal.signal(signal.SIGINT, lambda *_: agent.request_stop("user"))
    except ValueError:
        pass
    print(f"{C['dim']}Starting {args.mode} call… speak after the assistant greets you. Ctrl+C to hang up.{C['end']}")
    try:
        call_id = await agent.run()
        print(f"\nSaved as call #{call_id}. The worker will summarize it (python -m app.worker).")
    except AudioDeviceError as e:
        print(f"{C['red']}{e}{C['end']}")
    finally:
        printer.cancel()
        await db.close_pool()


async def check(_: argparse.Namespace) -> None:
    s = get_settings()
    print("✓ .env loaded (owner:", s.owner_name + ")")
    try:
        await db.open_pool(max_size=1)
        async with db.pool().connection() as conn:
            v = await (await conn.execute("select version() as v")).fetchone()
        print("✓ database:", (v or {}).get("v", "")[:60])
    except Exception as e:
        print("✗", db.describe_db_error(e))
    finally:
        await db.close_pool()
    from google import genai
    try:
        client = genai.Client(api_key=s.gemini_api_key)
        names = {m.name.split("/")[-1] async for m in await client.aio.models.list()}  # type: ignore[union-attr]
        for m in (s.live_model, s.postcall_model):
            print(("✓" if m in names else "?"), "model", m, "" if m in names else "(not in model list; check name)")
    except Exception as e:
        print("✗ Gemini:", e)
    from .audio.devices import main as devices_main
    devices_main()


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("talk", help="start a call from the terminal")
    t.add_argument("--mode", choices=["test", "phone"], default="test")
    t.add_argument("--direction", choices=["incoming", "outgoing"], default="incoming")
    t.add_argument("--name", default="")
    t.add_argument("--phone", default="")
    t.add_argument("--purpose", default="")
    t.add_argument("-v", "--verbose", action="store_true")
    c = sub.add_parser("check", help="verify configuration, database, Gemini and audio devices")
    c.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.use_selector_loop_on_windows()
    asyncio.run(talk(args) if args.cmd == "talk" else check(args))


if __name__ == "__main__":
    main()
