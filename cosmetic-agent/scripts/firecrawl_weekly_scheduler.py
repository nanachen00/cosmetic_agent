"""Run the approved BLINK daily-content crawler once per week."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"
STATUS_FILE = LOG_DIR / "firecrawl_scheduler_status.json"
LOCK_FILE = LOG_DIR / "firecrawl_scheduler.lock"
RUN_LOG = LOG_DIR / "firecrawl_weekly.log"
TIMEZONE = ZoneInfo("Asia/Taipei")
WEEKDAY = 0  # Monday
HOUR = 3
MINUTE = 30
FREQUENCY_LABEL = "每週一 03:30（Asia/Taipei）"
stopping = False


def next_run(now: datetime) -> datetime:
    candidate = now.replace(hour=HOUR, minute=MINUTE, second=0, microsecond=0)
    candidate += timedelta(days=(WEEKDAY - now.weekday()) % 7)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def write_status(**updates: object) -> None:
    current: dict[str, object] = {}
    if STATUS_FILE.exists():
        try:
            current = json.loads(STATUS_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            current = {}
    current.update(
        enabled=True,
        frequency=FREQUENCY_LABEL,
        pid=os.getpid(),
        updated_at=datetime.now(TIMEZONE).isoformat(),
        categories=["beauty_tip", "dermatologist_faq"],
        **updates,
    )
    temporary = STATUS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2))
    temporary.replace(STATUS_FILE)


def request_stop(_signum: int, _frame: object) -> None:
    global stopping
    stopping = True


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        lock.write(str(os.getpid()))
        lock.flush()
        scheduled = next_run(datetime.now(TIMEZONE))
        write_status(next_run_at=scheduled.isoformat(), last_result="waiting")
        while not stopping:
            now = datetime.now(TIMEZONE)
            if now >= scheduled:
                started = now.isoformat()
                with RUN_LOG.open("a") as output:
                    output.write(f"\n[{started}] weekly crawl started\n")
                    output.flush()
                    completed = subprocess.run(
                        [sys.executable, str(ROOT / "scripts" / "firecrawl_daily_content.py")],
                        cwd=ROOT,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        check=False,
                    )
                scheduled = next_run(datetime.now(TIMEZONE) + timedelta(minutes=1))
                write_status(
                    last_run_at=started,
                    last_result="success" if completed.returncode == 0 else "partial_failure",
                    last_exit_code=completed.returncode,
                    next_run_at=scheduled.isoformat(),
                )
            write_status(next_run_at=scheduled.isoformat())
            time.sleep(min(300, max(5, int((scheduled - datetime.now(TIMEZONE)).total_seconds()))))
    write_status(enabled=False, stopped_at=datetime.now(TIMEZONE).isoformat())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
