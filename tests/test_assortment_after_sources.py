from __future__ import annotations

import fcntl
import json
import signal
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from scripts import run_assortment_after_sources as runner


def _time(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 10, hour, minute, tzinfo=runner.MOSCOW)


def _logs(directory: Path) -> None:
    (directory / "competitor_matching_nightly.log").write_text(
        "[2026-09-10T04:10:01+03:00] starting competitor matching nightly\n"
        "[2026-09-10T04:17:00+03:00] competitor step finished: sync_onec_product_catalog\n"
        "[2026-09-10T04:18:00+03:00] competitor step failed: match_competitor_ftp\n"
    )
    (directory / "onec_stock_availability.log").write_text(
        "[2026-09-10T03:15:01+03:00] starting stock availability sync mode=nightly\n"
        "[2026-09-10T03:17:38+03:00] stock availability sync finished mode=nightly\n"
    )


@pytest.mark.parametrize("hour", [0, 3, 6, 9, 18])
def test_outside_window_does_not_read_logs(tmp_path, hour):
    assert runner.readiness(tmp_path, _time(hour))["status"] == "outside_night_window"


def test_catalog_success_does_not_depend_on_matching(tmp_path):
    _logs(tmp_path)
    assert runner.readiness(tmp_path, _time(4, 20))["status"] == "ready"


@pytest.mark.parametrize(
    "extra",
    [
        "[2026-09-10T04:19:00+03:00] starting competitor matching nightly\n",
        "[2026-09-10T04:19:00+03:00] competitor step failed: sync_onec_product_catalog\n",
        "[2026-09-10T05:19:00+03:00] competitor step finished: sync_onec_product_catalog\n",
        "[invalid] starting competitor matching nightly\n",
    ],
)
def test_new_or_invalid_catalog_attempt_blocks_previous_success(tmp_path, extra):
    _logs(tmp_path)
    with (tmp_path / "competitor_matching_nightly.log").open("a") as stream:
        stream.write(extra)
    assert runner.readiness(tmp_path, _time(4, 20))["status"] == "waiting_sources"


def test_yesterday_and_orphan_success_are_not_ready(tmp_path):
    _logs(tmp_path)
    catalog = tmp_path / "competitor_matching_nightly.log"
    catalog.write_text(catalog.read_text().replace("2026-09-10", "2026-09-09"))
    assert runner.readiness(tmp_path, _time(4, 20))["status"] == "waiting_sources"
    catalog.write_text(
        "[2026-09-10T04:17:00+03:00] competitor step finished: sync_onec_product_catalog\n"
    )
    assert runner.readiness(tmp_path, _time(4, 20))["status"] == "waiting_sources"


def test_missing_logs_block(tmp_path):
    with pytest.raises(FileNotFoundError):
        runner.readiness(tmp_path, _time(4, 20))


@pytest.mark.parametrize("exit_code", [0, 1, 124])
def test_one_attempt_per_day_and_original_script(tmp_path, monkeypatch, exit_code):
    _logs(tmp_path)

    class FixedTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return _time(4, 20)

    calls = []

    def refresh(repo_dir, log_dir, timeout):
        calls.append((repo_dir, log_dir, timeout))
        return exit_code

    monkeypatch.setattr(runner, "datetime", FixedTime)
    monkeypatch.setattr(runner, "run_refresh", refresh)
    state = tmp_path / "state.json"
    result = runner.execute(tmp_path, tmp_path, state)
    assert result["status"] == ("success" if exit_code == 0 else "failed")
    assert json.loads(state.read_text())["exit_code"] == exit_code
    assert calls == [(tmp_path, tmp_path, 6000)]
    assert runner.execute(tmp_path, tmp_path, state)["status"] == "already_attempted"
    assert len(calls) == 1


def test_corrupt_state_blocks_instead_of_restarting(tmp_path):
    state = tmp_path / "state.json"
    state.write_text("{")
    with pytest.raises(ValueError):
        runner.execute(tmp_path, tmp_path, state)


def test_lock_prevents_parallel_run(tmp_path):
    state = tmp_path / "state.json"
    with state.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert runner.execute(tmp_path, tmp_path, state)["status"] == "already_running"


def test_timeout_stops_only_child_process_group(tmp_path, monkeypatch):
    calls = []

    class Process:
        pid = 12345

        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("refresh", timeout)
            return -9

    def spawn(command, **kwargs):
        assert command == [
            "bash",
            str(tmp_path / "infra/cron/assortment_lifecycle_classification.sh"),
        ]
        assert kwargs["start_new_session"] is True
        return Process()

    monkeypatch.setattr(runner.subprocess, "Popen", spawn)
    monkeypatch.setattr(runner.os, "killpg", lambda *args: calls.append(args))
    assert runner.run_refresh(tmp_path, tmp_path, 10) == 124
    assert calls == [(12345, signal.SIGKILL)]
