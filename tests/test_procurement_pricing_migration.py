import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.models.procurement_pricing import ProcurementPriceBatch
from app.services.procurement_pricing import create_batch
from tests.test_procurement_pricing import payload


def test_migration_supports_real_batch_write_and_downgrade():
    path = (
        Path(__file__).resolve().parents[1] / "alembic/versions/f4065a001001_procurement_pricing.py"
    )
    spec = importlib.util.spec_from_file_location("pricing_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        with Session(connection) as db:
            batch = create_batch(db, payload(), "buyer")
            db.flush()
            assert db.get(ProcurementPriceBatch, batch.id).lines[0].new_price == "110"
        module.downgrade()
        assert inspect(connection).get_table_names() == []
