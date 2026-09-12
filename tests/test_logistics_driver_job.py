import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect, text

from tasks import sync_logistics_drivers_from_bitrix as job

PROJECT = Path(__file__).resolve().parents[1]


def test_migration_preserves_ids_and_supports_nullable_unique_binding():
    path = PROJECT / "alembic/versions/d6e8f0a2b4c6_logistics_driver_bitrix_sync.py"
    spec = importlib.util.spec_from_file_location("driver_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite://")
    metadata = MetaData()
    Table(
        "logistics_driver",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("full_name", String(255)),
    )
    Table("logistics_user", metadata, Column("id", Integer, primary_key=True))
    Table("logistics_draft", metadata, Column("id", Integer, primary_key=True))
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO logistics_driver (id, full_name) VALUES (5, 'Legacy')")
        )
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        assert connection.scalar(text("SELECT id FROM logistics_driver")) == 5
        assert connection.scalar(text("SELECT bitrix_user_id FROM logistics_driver")) is None
        assert inspect(connection).get_indexes("logistics_driver")[0]["unique"]
        module.downgrade()
        assert connection.scalar(text("SELECT full_name FROM logistics_driver")) == "Legacy"
    engine.dispose()


@pytest.mark.parametrize("use_credential_file", [False, True])
def test_cli_default_dry_run_and_explicit_apply(monkeypatch, capsys, tmp_path, use_credential_file):
    from types import SimpleNamespace

    settings = SimpleNamespace(
        logistics_driver_bitrix_sync_enabled=True,
        logistics_driver_bitrix_webhook_url="https://example.invalid/private",
    )
    if use_credential_file:
        credentials = tmp_path / "connector.env"
        credentials.write_text(
            "AI_TASK_ANALYSIS_BITRIX_WEBHOOK_BASE=https://example.invalid/private\n"
        )
        settings.logistics_driver_bitrix_webhook_url = None
        settings.logistics_driver_bitrix_webhook_env_file = str(credentials)
    monkeypatch.setattr(job, "get_settings", lambda: settings)
    monkeypatch.setattr(job, "get_engine", lambda: create_engine("sqlite://"))
    monkeypatch.setattr(job, "fetch_snapshot", lambda _: ([], 2))
    applies = []
    monkeypatch.setattr(
        job, "reconcile", lambda _session, _rows, apply: applies.append(apply) or {"created": 0}
    )
    monkeypatch.setattr("sys.argv", ["sync"])
    job.main()
    monkeypatch.setattr("sys.argv", ["sync", "--apply"])
    job.main()
    assert applies == [False, True]
    assert "private" not in capsys.readouterr().out
    settings.logistics_driver_bitrix_sync_enabled = False
    with pytest.raises(SystemExit, match="disabled"):
        job.main()


def test_cron_uses_active_release_lock_and_bounded_runtime():
    script = (PROJECT / "infra/cron/logistics_driver_sync.sh").read_text()
    cron = (PROJECT / "infra/cron/logistics_driver_sync.cron").read_text()
    assert "flock -n 9" in script
    assert "timeout 50" in script and "--apply" in script
    assert "LOGISTICS_DRIVER_BITRIX_SYNC_ENABLED:-false" in script
    assert "previous snapshot preserved" in script
    assert "* * * * * root" in cron and "pricing-service-task43-current" in cron
