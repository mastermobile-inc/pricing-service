"""Проверка обеспечения заказа резервом и проведённой ранней реализацией.

Адаптер обязан читать все факты в одной транзакции, связывать движение с
регистратором и заказом. Строка РТУ без товарного и заказного движения не факт
обеспечения. Этот модуль не читает БД и не разрешает оплату самостоятельно.
"""

from dataclasses import dataclass
from decimal import Decimal

ZERO_REF = bytes(16)
TOLERANCE = Decimal("0.001")
ItemKey = tuple[bytes, bytes, bytes]


@dataclass(frozen=True)
class CoverageLine:
    key: ItemKey
    quantity: Decimal  # Только единицы хранения.


@dataclass(frozen=True)
class SaleCoverage:
    document_ref: bytes
    order_ref: bytes
    key: ItemKey
    quantity: Decimal
    stock_outflow: Decimal
    order_outflow: Decimal
    posted: bool
    marked: bool
    origin_verified: bool


@dataclass(frozen=True)
class CoverageResult:
    covered: bool
    reason: str


def _quantity(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value >= 0


def _key(key: ItemKey) -> bool:
    return (
        len(key) == 3
        and all(isinstance(ref, bytes) and len(ref) == 16 for ref in key)
        and key[0] != ZERO_REF
    )


def _totals(lines: list[CoverageLine]) -> dict[ItemKey, Decimal]:
    totals: dict[ItemKey, Decimal] = {}
    for line in lines:
        if not _key(line.key) or not _quantity(line.quantity):
            raise ValueError("Invalid coverage quantity or identity")
        totals[line.key] = totals.get(line.key, Decimal(0)) + line.quantity
    return totals


def evaluate_document_coverage(
    *,
    order_ref: bytes,
    required: list[CoverageLine],
    remaining_demand: list[CoverageLine],
    reserves: list[CoverageLine],
    sales: list[SaleCoverage],
    has_returns: bool,
    snapshot_complete: bool,
) -> CoverageResult:
    """Fail closed; never count a sale and its stale reserve twice."""
    if not snapshot_complete or len(order_ref) != 16 or order_ref == ZERO_REF:
        return CoverageResult(False, "document_evidence_incomplete")
    if has_returns:
        return CoverageResult(False, "document_returns_require_review")
    try:
        expected = _totals(required)
        remaining = _totals(remaining_demand)
        reserved = _totals(reserves)
    except ValueError:
        return CoverageResult(False, "document_evidence_invalid")
    if not expected or any(q <= 0 for q in expected.values()):
        return CoverageResult(False, "document_order_lines_invalid")
    sold: dict[ItemKey, Decimal] = {}
    seen: set[tuple[bytes, ItemKey]] = set()
    for sale in sales:
        identity = (sale.document_ref, sale.key)
        if (
            len(sale.document_ref) != 16
            or sale.document_ref == ZERO_REF
            or sale.order_ref != order_ref
            or not _key(sale.key)
            or not sale.posted
            or sale.marked
            or not sale.origin_verified
            or identity in seen
            or not all(
                _quantity(q) for q in (sale.quantity, sale.stock_outflow, sale.order_outflow)
            )
            or sale.quantity <= 0
        ):
            return CoverageResult(False, "document_sale_not_proven")
        seen.add(identity)
        if (
            abs(sale.quantity - sale.stock_outflow) > TOLERANCE
            or abs(sale.quantity - sale.order_outflow) > TOLERANCE
        ):
            return CoverageResult(False, "document_movements_mismatch")
        sold[sale.key] = sold.get(sale.key, Decimal(0)) + sale.quantity
    if not sales:
        return CoverageResult(False, "document_sales_missing")
    if (set(remaining) | set(reserved) | set(sold)) - set(expected):
        return CoverageResult(False, "document_extra_items")
    for key, quantity in expected.items():
        fulfilled = sold.get(key, Decimal(0))
        demand = remaining.get(key, Decimal(0))
        reserve = reserved.get(key, Decimal(0))
        if fulfilled - quantity > TOLERANCE or abs(quantity - fulfilled - demand) > TOLERANCE:
            return CoverageResult(False, "document_demand_mismatch")
        if reserve - demand > TOLERANCE:
            return CoverageResult(False, "document_reserve_overlap")
        if abs(fulfilled + reserve - quantity) > TOLERANCE:
            return CoverageResult(False, "document_coverage_partial")
    return CoverageResult(True, "amount_and_full_document_coverage_match")
