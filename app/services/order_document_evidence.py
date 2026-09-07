"""Read-only UT 10.3 document evidence in the caller's SERIALIZABLE transaction.

Physical names verified through production ПолучитьСтруктуруХраненияБазыДанных
07.09.2026. No NOLOCK, side connections, or business writes are permitted here.
"""

from collections import defaultdict
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from app.services.order_document_coverage import (
    TOLERANCE,
    CoverageLine,
    CoverageResult,
    SaleCoverage,
    evaluate_document_coverage,
)

SALES_SQL = """
SELECT d._IDRRef AS document_ref, d._Fld4939_RRRef AS order_ref, d._Fld4939_RTRef AS order_type,
 v._Fld4989RRef AS line_order_ref, v._Fld4974RRef AS product_ref,
 v._Fld4981RRef AS characteristic_ref, v._Fld4976RRef AS series_ref,
 v._Fld4983RRef AS warehouse_ref, v._Fld4971 AS quantity,
 v._Fld4973 AS coefficient, u._Fld550 AS storage_coefficient
FROM dbo._Document203 d
JOIN (
 SELECT _IDRRef AS document_ref FROM dbo._Document203 WHERE _Fld4939_RRRef=0x{order_ref_hex}
 UNION
 SELECT _Document203_IDRRef FROM dbo._Document203_VT4966 WHERE _Fld4989RRef=0x{order_ref_hex}
) selected ON selected.document_ref=d._IDRRef
JOIN dbo._Document203_VT4966 v ON v._Document203_IDRRef=d._IDRRef
JOIN dbo._Reference62 p ON p._IDRRef=v._Fld4974RRef
JOIN dbo._Reference41 u ON u._IDRRef=p._Fld843RRef
WHERE d._Posted=0x01 AND d._Marked=0x00
 AND (d._Fld4939_RRRef=0x{order_ref_hex} OR v._Fld4989RRef=0x{order_ref_hex})
"""
STOCK_SQL = """
SELECT m._RecorderRRef AS document_ref, m._Fld7726RRef AS warehouse_ref,
 m._Fld7727RRef AS product_ref, m._Fld7728RRef AS characteristic_ref,
 m._Fld7729RRef AS series_ref,
 SUM(CASE WHEN m._RecordKind=1 THEN m._Fld7731 ELSE -m._Fld7731 END) AS quantity
FROM dbo._AccumRg7725 m
JOIN dbo._Document203 d ON d._IDRRef=m._RecorderRRef
WHERE m._RecorderTRef=0x000000cb AND m._Active=0x01
 AND d._Posted=0x01 AND d._Marked=0x00 AND d._Fld4939_RRRef=0x{order_ref_hex}
GROUP BY m._RecorderRRef,m._Fld7726RRef,m._Fld7727RRef,m._Fld7728RRef,m._Fld7729RRef
"""
ORDER_MOVEMENTS_SQL = """
SELECT m._RecorderRRef AS document_ref,m._Fld7131RRef AS product_ref,
 m._Fld7132RRef AS characteristic_ref,
 SUM(CASE WHEN m._RecordKind=1 THEN m._Fld7140 ELSE -m._Fld7140 END) AS quantity
FROM dbo._AccumRg7127 m
JOIN dbo._Document203 d ON d._IDRRef=m._RecorderRRef
WHERE m._RecorderTRef=0x000000cb AND m._Active=0x01
 AND d._Posted=0x01 AND d._Marked=0x00
 AND m._Fld7129RRef=0x{order_ref_hex} AND m._Fld7131RRef IN (SELECT _Fld2434RRef FROM dbo._Document132_VT2427 WHERE _Document132_IDRRef=0x{order_ref_hex})
GROUP BY m._RecorderRRef,m._Fld7131RRef,m._Fld7132RRef
"""
DEMAND_SQL = """
SELECT m._Fld7131RRef AS product_ref,m._Fld7132RRef AS characteristic_ref,
 SUM(CASE WHEN m._RecordKind=0 THEN m._Fld7140 ELSE -m._Fld7140 END) AS quantity
FROM dbo._AccumRg7127 m
WHERE m._Active=0x01 AND m._Fld7129RRef=0x{order_ref_hex} AND m._Fld7131RRef IN (SELECT _Fld2434RRef FROM dbo._Document132_VT2427 WHERE _Document132_IDRRef=0x{order_ref_hex})
GROUP BY m._Fld7131RRef,m._Fld7132RRef
"""
RETURNS_SQL = """
WITH related_sales AS (
 SELECT _IDRRef AS document_ref FROM dbo._Document203
 WHERE _Fld4939_RRRef=0x{order_ref_hex}
 UNION
 SELECT _Document203_IDRRef FROM dbo._Document203_VT4966
 WHERE _Fld4989RRef=0x{order_ref_hex}
), related_returns AS (
 SELECT _IDRRef AS document_ref FROM dbo._Document109
 WHERE _Fld1684_RRRef=0x{order_ref_hex}
 UNION
 SELECT _Document109_IDRRef FROM dbo._Document109_VT1698
 WHERE _Fld1719RRef=0x{order_ref_hex}
 UNION
 SELECT d._IDRRef FROM dbo._Document109 d
 JOIN related_sales s ON s.document_ref=d._Fld1684_RRRef
 UNION
 SELECT v._Document109_IDRRef FROM dbo._Document109_VT1698 v
 JOIN related_sales s ON s.document_ref=v._Fld1712_RRRef
 WHERE v._Fld1712_RTRef=0x000000cb
)
SELECT TOP (1) d._IDRRef AS document_ref
FROM related_returns r
JOIN dbo._Document109 d ON d._IDRRef=r.document_ref
WHERE d._Posted=0x01 AND d._Marked=0x00
"""


def _ref(value: Any) -> bytes:
    result = bytes(value or b"")
    if len(result) != 16:
        raise ValueError("Invalid document evidence reference")
    return result


def _quantity(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("Invalid document evidence quantity")
    return result


def _rows(connection: Any, sql: str, order_ref: bytes) -> list[Any]:
    key = _ref(order_ref).hex()
    return list(connection.execute(text(sql.format(order_ref_hex=key))).mappings())


def fetch_document_coverage(
    connection: Any,
    *,
    order_ref: bytes,
    lines: list[Any],
    reserves: list[Any],
    expected_warehouse_ref: bytes,
) -> CoverageResult:
    # Goods only. The existing payment check still compares the complete order sum.
    required = []
    for line in lines:
        if line.storage_coefficient <= 0 or line.coefficient <= 0:
            return CoverageResult(False, "document_evidence_invalid")
        required.append(
            CoverageLine(
                (line.product_ref, line.characteristic_ref, line.series_ref),
                line.quantity * line.coefficient / line.storage_coefficient,
            )
        )
    if any(r.warehouse_ref != expected_warehouse_ref for r in reserves):
        return CoverageResult(False, "document_reserve_warehouse_mismatch")
    reserved = [
        CoverageLine((r.product_ref, r.characteristic_ref, r.series_ref), r.quantity)
        for r in reserves
    ]
    if _rows(connection, RETURNS_SQL, order_ref):
        return CoverageResult(False, "document_returns_require_review")
    sale_rows = _rows(connection, SALES_SQL, order_ref)
    if not sale_rows:
        return CoverageResult(False, "document_sales_missing")
    by_stock = defaultdict(Decimal)
    by_doc = defaultdict(Decimal)
    by_order = defaultdict(Decimal)
    for row in sale_rows:
        if (
            bytes(row["order_type"]) != bytes.fromhex("00000084")
            or _ref(row["order_ref"]) != order_ref
            or _ref(row["line_order_ref"]) != order_ref
        ):
            return CoverageResult(False, "document_sale_link_mismatch")
        coefficient = _quantity(row["coefficient"])
        storage = _quantity(row["storage_coefficient"])
        if coefficient <= 0 or storage <= 0:
            return CoverageResult(False, "document_evidence_invalid")
        quantity = _quantity(row["quantity"]) * coefficient / storage
        doc = _ref(row["document_ref"])
        key = (_ref(row["product_ref"]), _ref(row["characteristic_ref"]), _ref(row["series_ref"]))
        warehouse = _ref(row["warehouse_ref"])
        if warehouse == bytes(16):
            return CoverageResult(False, "document_evidence_invalid")
        by_stock[(doc, warehouse, *key)] += quantity
        by_doc[(doc, *key)] += quantity
        by_order[(doc, *key[:2])] += quantity
    actual_stock = {}
    for row in _rows(connection, STOCK_SQL, order_ref):
        key = tuple(
            _ref(row[name])
            for name in [
                "document_ref",
                "warehouse_ref",
                "product_ref",
                "characteristic_ref",
                "series_ref",
            ]
        )
        actual_stock[key] = _quantity(row["quantity"])
    actual_order = {}
    for row in _rows(connection, ORDER_MOVEMENTS_SQL, order_ref):
        key = tuple(
            _ref(row[name]) for name in ["document_ref", "product_ref", "characteristic_ref"]
        )
        actual_order[key] = _quantity(row["quantity"])
    for expected, actual in [(by_stock, actual_stock), (by_order, actual_order)]:
        if set(expected) != set(actual) or any(
            abs(q - actual[k]) > TOLERANCE for k, q in expected.items()
        ):
            return CoverageResult(False, "document_movements_mismatch")
    # The order register has no series. Verify its aggregate before distributing
    # demand by the series explicitly identified by sale and order lines.
    outstanding = defaultdict(Decimal)
    for line in required:
        outstanding[line.key] += line.quantity
    for key, quantity in by_doc.items():
        outstanding[key[1:]] -= quantity
    expected_demand = defaultdict(Decimal)
    for key, quantity in outstanding.items():
        expected_demand[key[:2]] += quantity
    actual_demand = {}
    for row in _rows(connection, DEMAND_SQL, order_ref):
        key = (_ref(row["product_ref"]), _ref(row["characteristic_ref"]))
        actual_demand[key] = _quantity(row["quantity"])
    all_keys = set(expected_demand) | set(actual_demand)
    if any(
        abs(expected_demand.get(k, Decimal(0)) - actual_demand.get(k, Decimal(0))) > TOLERANCE
        for k in all_keys
    ):
        return CoverageResult(False, "document_demand_mismatch")
    sales = [
        SaleCoverage(k[0], order_ref, k[1:], q, q, q, True, False, True) for k, q in by_doc.items()
    ]
    return evaluate_document_coverage(
        order_ref=order_ref,
        required=required,
        remaining_demand=[CoverageLine(k, q) for k, q in outstanding.items()],
        reserves=reserved,
        sales=sales,
        has_returns=False,
        snapshot_complete=True,
    )
