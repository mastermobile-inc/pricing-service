import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect


def test_scan_accounting_migration_retains_evidence_on_rollback():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/d0f2a4b6c8e0_logistics_scan_accounting.py"
    )
    spec = importlib.util.spec_from_file_location("scan_accounting_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "c9e1a3b5d7f2"
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for name in ("logistics_order_plan_unit", "logistics_transfer_event", "logistics_user"):
        Table(name, metadata, Column("id", Integer, primary_key=True))
    metadata.create_all(engine)
    with engine.begin() as connection:
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        assert {"logistics_accounting_event", "logistics_receipt_check"}.issubset(
            inspect(connection).get_table_names()
        )
        assert any(
            set(item["column_names"]) == {"unit_id", "operation"}
            for item in inspect(connection).get_unique_constraints("logistics_accounting_event")
        )
        with pytest.raises(RuntimeError, match="retain"):
            module.downgrade()
        assert "logistics_accounting_event" in inspect(connection).get_table_names()
    engine.dispose()
