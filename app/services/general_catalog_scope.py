from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select

from app.models.product import Product

GENERAL_CATALOG_GATE_REASON = "gate_not_in_general_catalog"


def general_catalog_product_condition(nomenclature_code):
    return (
        select(Product.id)
        .where(
            Product.code_1c == nomenclature_code,
            Product.code_1c.is_not(None),
            Product.code_1c != "",
            Product.is_active.is_(True),
            Product.is_marked_for_deletion.is_(False),
        )
        .exists()
    )


def general_catalog_product_codes(connection) -> set[str]:
    return set(
        connection.execute(
            select(Product.code_1c).where(
                Product.code_1c.is_not(None),
                Product.code_1c != "",
                Product.is_active.is_(True),
                Product.is_marked_for_deletion.is_(False),
            )
        ).scalars()
    )


def filter_general_catalog_records(
    connection, records: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    allowed_codes = general_catalog_product_codes(connection)
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for record in records:
        target = included if record.get("nomenclature_code") in allowed_codes else excluded
        target.append(dict(record))
    return included, excluded


def require_general_catalog_codes(connection, codes: Sequence[str | None]) -> None:
    allowed_codes = general_catalog_product_codes(connection)
    excluded = sorted({str(code or "") for code in codes if code not in allowed_codes})
    if excluded:
        raise ValueError(f"{GENERAL_CATALOG_GATE_REASON}: " + ", ".join(excluded))
