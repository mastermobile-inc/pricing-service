"""Execute production query logic against fixtures; only SQL dialect is translated."""

import re
import sqlite3

import pytest

from app.services.order_document_evidence import DEMAND_SQL, RETURNS_SQL
from app.services.order_payment_control import ONEC_ORDER_RESERVES_SQL_TEMPLATE

ORDER = bytes.fromhex("11" * 16)
SALE = bytes.fromhex("22" * 16)
PRODUCT = bytes.fromhex("33" * 16)
ZERO = bytes(16)


def execute(db, sql):
    sql = sql.format(order_ref_hex=ORDER.hex()).replace("dbo.", "")
    sql = sql.replace("TOP (1)", "")
    sql = re.sub(r"0x([0-9a-fA-F]+)", r"X'\1'", sql)
    return db.execute(sql).fetchall()


def test_current_demand_uses_active_movements_across_month_boundary():
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE _Document132_VT2427 (_Document132_IDRRef BLOB, _Fld2434RRef BLOB);
        CREATE TABLE _AccumRg7127 (_Period TEXT, _Active BLOB, _RecordKind INT,
            _Fld7129RRef BLOB, _Fld7131RRef BLOB, _Fld7132RRef BLOB, _Fld7140 NUMERIC);
    """)
    db.execute("INSERT INTO _Document132_VT2427 VALUES (?,?)", (ORDER, PRODUCT))
    for period, active, kind, quantity in [
        ("2026-08-31", b"\x01", 0, 3),
        ("2026-09-07", b"\x01", 1, 2),
        ("2026-09-07", b"\x00", 1, 50),
    ]:
        db.execute(
            "INSERT INTO _AccumRg7127 VALUES (?,?,?,?,?,?,?)",
            (period, active, kind, ORDER, PRODUCT, ZERO, quantity),
        )
    assert execute(db, DEMAND_SQL) == [(PRODUCT, ZERO, 1)]


def test_current_reserve_ignores_inactive_and_excludes_consumed_warehouse():
    db = sqlite3.connect(":memory:")
    db.execute("""CREATE TABLE _AccumRg7653 (_Period TEXT, _Active BLOB, _RecordKind INT,
        _Fld7657_RTRef BLOB, _Fld7657_RRRef BLOB, _Fld7654RRef BLOB,
        _Fld7655RRef BLOB, _Fld7656RRef BLOB, _Fld7658RRef BLOB, _Fld7659 NUMERIC)""")
    w1, w2 = b"a" * 16, b"b" * 16
    for period, active, kind, warehouse, quantity in [
        ("2026-08-31", b"\x01", 0, w1, 3),
        ("2026-09-07", b"\x01", 1, w1, 3),
        ("2026-09-07", b"\x01", 0, w2, 1),
        ("2026-09-07", b"\x00", 0, w2, 50),
    ]:
        db.execute(
            "INSERT INTO _AccumRg7653 VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                period,
                active,
                kind,
                bytes.fromhex("00000084"),
                ORDER,
                warehouse,
                PRODUCT,
                ZERO,
                ZERO,
                quantity,
            ),
        )
    assert execute(db, ONEC_ORDER_RESERVES_SQL_TEMPLATE) == [(w2, PRODUCT, ZERO, ZERO, 1)]


@pytest.mark.parametrize(
    "link", ["order_header", "order_line", "sale_header", "sale_batch", "sale_line_batch"]
)
def test_return_links_are_detected_before_documentary_payment(link):
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE _Document203 (_IDRRef BLOB, _Fld4939_RTRef BLOB, _Fld4939_RRRef BLOB);
        CREATE TABLE _Document203_VT4966 (_Document203_IDRRef BLOB, _Fld4989RRef BLOB);
        CREATE TABLE _Document109 (_IDRRef BLOB, _Posted BLOB, _Marked BLOB, _Fld1684_RRRef BLOB);
        CREATE TABLE _Document109_VT1698 (_Document109_IDRRef BLOB, _Fld1719RRef BLOB,
            _Fld1712_RTRef BLOB, _Fld1712_RRRef BLOB);
    """)
    ret = b"r" * 16
    db.execute(
        "INSERT INTO _Document203 VALUES (?,?,?)",
        (SALE, bytes.fromhex("00000084"), ZERO if link == "sale_line_batch" else ORDER),
    )
    if link == "sale_line_batch":
        db.execute("INSERT INTO _Document203_VT4966 VALUES (?,?)", (SALE, ORDER))
    db.execute(
        "INSERT INTO _Document109 VALUES (?,?,?,?)",
        (
            ret,
            b"\x01",
            b"\x00",
            ORDER if link == "order_header" else SALE if link == "sale_header" else ZERO,
        ),
    )
    db.execute(
        "INSERT INTO _Document109_VT1698 VALUES (?,?,?,?)",
        (
            ret,
            ORDER if link == "order_line" else ZERO,
            bytes.fromhex("000000cb"),
            SALE if link in {"sale_batch", "sale_line_batch"} else ZERO,
        ),
    )
    assert execute(db, RETURNS_SQL) == [(ret,)]
    db.execute("UPDATE _Document109 SET _Posted=?", (b"\x00",))
    assert execute(db, RETURNS_SQL) == []
