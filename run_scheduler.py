#!/usr/bin/env python3
"""Run scheduler daemon independently from CLI/UI."""

from __future__ import annotations

import os
import signal
import sys
import time
from datetime import datetime

from scheduler_service import AnalysisScheduler

PID_FILE = "scheduler.pid"


def save_pid() -> None:
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


def load_pid() -> int | None:
    if not os.path.exists(PID_FILE):
        return None
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None


def remove_pid() -> None:
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)


def is_running() -> bool:
    pid = load_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop_scheduler() -> bool:
    pid = load_pid()
    if pid is None:
        print("No scheduler PID found.")
        return False

    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            try:
                os.kill(pid, 0)
                time.sleep(0.5)
            except OSError:
                remove_pid()
                print("Scheduler stopped.")
                return True
        os.kill(pid, signal.SIGKILL)
        remove_pid()
        print("Scheduler force-stopped.")
        return True
    except OSError as e:
        print(f"Failed to stop scheduler: {e}")
        remove_pid()
        return False


def signal_handler(signum, frame):
    print(f"[{datetime.now()}] signal={signum} shutting down")
    remove_pid()
    sys.exit(0)


def run_scheduler() -> None:
    if is_running():
        print(f"Scheduler already running (PID={load_pid()})")
        sys.exit(1)

    save_pid()
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    scheduler = AnalysisScheduler()
    jobs = scheduler.list_jobs("UTC")

    print(f"[{datetime.now()}] Scheduler started PID={os.getpid()}")
    print(f"[{datetime.now()}] Loaded {len(jobs)} jobs")
    for job in jobs:
        print(f"  - {job['ticker']} @ {job['schedule_utc']} (next {job['next_run_local']})")

    try:
        while True:
            time.sleep(1)
    finally:
        remove_pid()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "stop":
        stop_scheduler()
    else:
        run_scheduler()


if __name__ == "__main__":
    main()
