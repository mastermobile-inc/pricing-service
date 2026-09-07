from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.services import order_document_evidence as repo
from app.services.order_payment_control import OneCOrderLine, OneCOrderReserve

ORDER_REF = b"o" * 16
P = b"p" * 16
W = b"w" * 16
Z = bytes(16)
DOC = b"d" * 16
LINE = OneCOrderLine(1, P, Z, Z, W, b"u" * 16, b"u" * 16, D(1), D(1), D(1))


def rows():
    return {
        repo.RETURNS_SQL: [],
        repo.SALES_SQL: [
            dict(
                document_ref=DOC,
                order_ref=ORDER_REF,
                order_type=bytes.fromhex("00000084"),
                line_order_ref=ORDER_REF,
                product_ref=P,
                characteristic_ref=Z,
                series_ref=Z,
                warehouse_ref=W,
                quantity=1,
                coefficient=1,
                storage_coefficient=1,
            )
        ],
        repo.STOCK_SQL: [
            dict(
                document_ref=DOC,
                warehouse_ref=W,
                product_ref=P,
                characteristic_ref=Z,
                series_ref=Z,
                quantity=1,
            )
        ],
        repo.ORDER_MOVEMENTS_SQL: [
            dict(document_ref=DOC, product_ref=P, characteristic_ref=Z, quantity=1)
        ],
        repo.DEMAND_SQL: [],
    }


def evaluate(monkeypatch, data, lines=None, reserves=None):
    token = object()
    calls = []

    def read(connection, sql, order):
        assert connection is token and order == ORDER_REF
        calls.append(sql)
        return data[sql]

    monkeypatch.setattr(repo, "_rows", read)
    return repo.fetch_document_coverage(
        token,
        order_ref=ORDER_REF,
        lines=lines or [LINE],
        reserves=reserves or [],
        expected_warehouse_ref=W,
    )


def test_same_snapshot_checks_goods_stock_order_and_demand(monkeypatch):
    assert evaluate(monkeypatch, rows()).covered


@pytest.mark.parametrize(
    "sql,field,value",
    [
        (repo.SALES_SQL, "order_type", bytes.fromhex("000000cb")),
        (repo.SALES_SQL, "line_order_ref", b"x" * 16),
        (repo.STOCK_SQL, "warehouse_ref", b"x" * 16),
        (repo.STOCK_SQL, "quantity", 0),
        (repo.ORDER_MOVEMENTS_SQL, "quantity", 0),
    ],
)
def test_different_order_or_movements_never_suffice(monkeypatch, sql, field, value):
    data = rows()
    data[sql][0][field] = value
    assert not evaluate(monkeypatch, data).covered


def test_returns_block_before_sale_checks(monkeypatch):
    data = rows()
    data[repo.RETURNS_SQL] = [dict(document_ref=b"r" * 16)]
    assert evaluate(monkeypatch, data).reason == "document_returns_require_review"


def test_missing_stock_movement_is_not_a_sale(monkeypatch):
    data = rows()
    data[repo.STOCK_SQL] = []
    assert not evaluate(monkeypatch, data).covered


def test_two_series_checked_against_seriesless_order_register(monkeypatch):
    data = rows()
    s = b"s" * 16
    data[repo.SALES_SQL].append(dict(data[repo.SALES_SQL][0], series_ref=s))
    data[repo.STOCK_SQL].append(dict(data[repo.STOCK_SQL][0], series_ref=s))
    data[repo.ORDER_MOVEMENTS_SQL][0]["quantity"] = 2
    assert evaluate(monkeypatch, data, lines=[LINE, replace(LINE, series_ref=s)]).covered
    data[repo.ORDER_MOVEMENTS_SQL][0]["quantity"] = 1
    assert not evaluate(monkeypatch, data, lines=[LINE, replace(LINE, series_ref=s)]).covered


def test_stale_reserve_does_not_double_coverage(monkeypatch):
    r = OneCOrderReserve(W, P, Z, Z, D(1))
    assert evaluate(monkeypatch, rows(), reserves=[r]).reason == "document_reserve_overlap"
