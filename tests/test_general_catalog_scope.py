import pytest
from sqlalchemy import create_engine, select

from app.models.product import Product
from app.services.general_catalog_scope import (
    filter_general_catalog_records,
    general_catalog_product_condition,
    require_general_catalog_codes,
)


def test_local_catalog_gate_requires_active_exact_onec_code():
    engine = create_engine("sqlite://")
    Product.__table__.create(engine)
    with engine.begin() as connection:
        connection.execute(
            Product.__table__.insert(),
            [
                {
                    "article": "1",
                    "name": "Дисплей",
                    "code_1c": "allowed",
                    "is_active": True,
                    "is_marked_for_deletion": False,
                },
                {
                    "article": "2",
                    "name": "АКБ",
                    "code_1c": "battery",
                    "is_active": True,
                    "is_marked_for_deletion": False,
                },
                {
                    "article": "3",
                    "name": "Дисплей",
                    "code_1c": "inactive",
                    "is_active": False,
                    "is_marked_for_deletion": False,
                },
                {
                    "article": "4",
                    "name": "Дисплей",
                    "code_1c": "deleted",
                    "is_active": True,
                    "is_marked_for_deletion": True,
                },
                {
                    "article": "5",
                    "name": "Дисплей",
                    "code_1c": None,
                    "is_active": True,
                    "is_marked_for_deletion": False,
                },
            ],
        )
        codes = ["allowed", "battery", "inactive", "deleted", "missing", ""]
        included, excluded = filter_general_catalog_records(
            connection, [{"nomenclature_code": code} for code in codes]
        )
        assert [row["nomenclature_code"] for row in included] == ["allowed", "battery"]
        assert len(excluded) == 4
        for code in codes:
            assert bool(connection.scalar(select(general_catalog_product_condition(code)))) == (
                code in {"allowed", "battery"}
            )
        require_general_catalog_codes(connection, ["allowed", "battery"])
        with pytest.raises(ValueError, match="gate_not_in_general_catalog"):
            require_general_catalog_codes(connection, ["missing"])
    engine.dispose()
