from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
SOURCE_MARKERS = {
    "catalog": (
        "competitor_matching_nightly.log",
        "starting competitor matching nightly",
        "competitor step finished: sync_onec_product_catalog",
        "competitor step failed: sync_onec_product_catalog",
    ),
    "availability": (
        "onec_stock_availability.log",
        "starting stock availability sync mode=",
        "stock availability sync finished mode=",
        "stock availability sync failed",
    ),
}


def source_success(log_dir: Path, source: str, now: datetime) -> str | None:
    filename, start, success, failure = SOURCE_MARKERS[source]
    started = None
    finished = None
    with (log_dir / filename).open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            line = line.lstrip("\x00")
            if not line.startswith("[") or "] " not in line:
                continue
            stamp, message = line[1:].split("] ", 1)
            if not any(marker in message for marker in (start, success, failure)):
                continue
            try:
                event_time = datetime.fromisoformat(stamp)
            except ValueError:
                return None
            if event_time.tzinfo is None or event_time > now:
                return None
            if event_time.astimezone(MOSCOW).date() != now.astimezone(MOSCOW).date():
                continue
            if start in message:
                started = event_time
                finished = None
            elif failure in message:
                started = None
                finished = None
            elif started is not None and event_time >= started:
                finished = event_time.isoformat()
    return finished


def readiness(log_dir: Path, now: datetime) -> dict:
    now = now.astimezone(MOSCOW)
    if not 4 <= now.hour < 6:
        return {"status": "outside_night_window"}
    sources = {source: source_success(log_dir, source, now) for source in SOURCE_MARKERS}
    return {
        "status": "ready" if all(sources.values()) else "waiting_sources",
        "sources": sources,
    }


def write_state(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def run_refresh(repo_dir: Path, log_dir: Path, timeout: float) -> int:
    process = subprocess.Popen(
        ["bash", str(repo_dir / "infra/cron/assortment_lifecycle_classification.sh")],
        cwd=repo_dir,
        env={**os.environ, "REPO_DIR": str(repo_dir), "LOG_DIR": str(log_dir)},
        start_new_session=True,
    )
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        return 124


def execute(repo_dir: Path, log_dir: Path, state_file: Path) -> dict:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with state_file.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already_running"}
        now = datetime.now(MOSCOW)
        if state_file.exists():
            previous = json.loads(state_file.read_text(encoding="utf-8"))
            if not isinstance(previous, dict) or not previous.get("date"):
                raise ValueError("invalid nightly state")
            if previous["date"] >= now.date().isoformat():
                return {"status": "already_attempted", "previous": previous}
        result = readiness(log_dir, now)
        if result["status"] != "ready":
            return result
        deadline = now.replace(hour=6, minute=0, second=0, microsecond=0)
        payload = {
            **result,
            "date": now.date().isoformat(),
            "started_at": now.isoformat(),
            "status": "running",
        }
        write_state(state_file, payload)
        timeout = (deadline - datetime.now(MOSCOW)).total_seconds()
        if timeout <= 0:
            exit_code = 124
        else:
            exit_code = run_refresh(repo_dir, log_dir, timeout)
        payload.update(
            status="success" if exit_code == 0 else "failed",
            exit_code=exit_code,
            finished_at=datetime.now(MOSCOW).isoformat(),
        )
        write_state(state_file, payload)
        return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=Path(os.environ.get("REPO_DIR", "/opt/MM/pricing-service-task43-current")),
    )
    parser.add_argument(
        "--log-dir", type=Path, default=Path(os.environ.get("LOG_DIR", "/var/log/pricing"))
    )
    parser.add_argument(
        "--state-file", type=Path, default=Path("/var/lib/pricing/assortment-nightly.json")
    )
    args = parser.parse_args()
    try:
        result = (
            readiness(args.log_dir, datetime.now(MOSCOW))
            if args.check
            else execute(args.repo_dir.resolve(), args.log_dir, args.state_file)
        )
    except (OSError, ValueError) as error:
        result = {"status": "blocked", "error_type": type(error).__name__}
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["status"] in {"blocked", "failed"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
