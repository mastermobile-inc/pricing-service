import fcntl
import os
import subprocess
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def test_cron_lock_apply_only_marker_and_failure(tmp_path):
    repo = tmp_path / "release"
    python = repo / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/bash\necho invoked >> "$CALLS_FILE"\nexit "${FAKE_STATUS:-0}"\n')
    python.chmod(0o755)
    marker, lock, calls = (tmp_path / name for name in ("success", "lock", "calls"))
    env = {
        **os.environ,
        "REPO_DIR": str(repo),
        "CALLS_FILE": str(calls),
        "LOGISTICS_TRANSFER_SYNC_LOG_FILE": str(tmp_path / "sync.log"),
        "LOGISTICS_TRANSFER_SYNC_SUCCESS_FILE": str(marker),
        "LOGISTICS_TRANSFER_SYNC_LOCK_FILE": str(lock),
        "LOGISTICS_TRANSFER_SYNC_APPLY": "false",
    }
    script = PROJECT / "infra/cron/logistics_transfer_sync.sh"

    def run():
        return subprocess.run(["bash", str(script)], env=env, capture_output=True, timeout=5)

    assert run().returncode == 0 and not marker.exists()
    env["LOGISTICS_TRANSFER_SYNC_APPLY"] = "true"
    assert run().returncode == 0 and marker.exists()
    os.utime(marker, (1000, 1000))
    env["FAKE_STATUS"] = "1"
    assert run().returncode == 1 and marker.stat().st_mtime == 1000
    before = calls.read_text()
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run().returncode == 0
    assert calls.read_text() == before and marker.stat().st_mtime == 1000


def test_watchdog_separate_freshness(tmp_path):
    marker = tmp_path / "transfer-success"
    env = {**os.environ, "LOGISTICS_TRANSFER_SYNC_SUCCESS_FILE": str(marker)}
    script = PROJECT / "infra/cron/logistics_transfer_sync_watchdog.sh"

    def run():
        return subprocess.run(["bash", str(script)], env=env, capture_output=True, timeout=5)

    assert run().returncode == 1
    marker.touch()
    assert run().returncode == 0
    stamp = time.time() - 181
    os.utime(marker, (stamp, stamp))
    assert run().returncode == 1
