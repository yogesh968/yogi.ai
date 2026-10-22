"""One command to run everything:  python run.py

Starts the dashboard server and the background worker as two processes,
restarts either if it crashes, and stops both on Ctrl+C.
"""
from __future__ import annotations

import subprocess
import sys
import time
import webbrowser

from app.config import get_settings

PROCS = {"server": [sys.executable, "-m", "app.server"], "worker": [sys.executable, "-m", "app.worker"]}


def main() -> None:
    s = get_settings()  # fail fast with a friendly message if .env is wrong
    subprocess.run([sys.executable, "-m", "app.migrate"], check=True)
    running = {name: subprocess.Popen(cmd) for name, cmd in PROCS.items()}
    if "--no-browser" not in sys.argv:
        time.sleep(2.5)
        webbrowser.open(f"http://{s.host}:{s.port}")
    crashes = {name: 0 for name in PROCS}
    try:
        while True:
            time.sleep(1)
            for name, p in list(running.items()):
                code = p.poll()
                if code is None:
                    continue
                crashes[name] += 1
                if crashes[name] > 5:
                    print(f"[run] {name} keeps crashing (exit {code}); giving up. Check the log above.")
                    raise KeyboardInterrupt
                print(f"[run] {name} exited with {code}; restarting in 3 s…")
                time.sleep(3)
                running[name] = subprocess.Popen(PROCS[name])
    except KeyboardInterrupt:
        print("\n[run] stopping…")
    finally:
        # Ctrl+C is delivered to the children too, so give them time to save any live call cleanly.
        for p in running.values():
            try:
                p.wait(timeout=25)
            except subprocess.TimeoutExpired:
                p.terminate()


if __name__ == "__main__":
    main()
