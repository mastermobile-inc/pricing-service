from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.services.order_document_coverage import (
    CoverageLine,
    SaleCoverage,
    evaluate_document_coverage,
)

ORDER = b"o" * 16
KEY = (b"p" * 16, bytes(16), bytes(16))
SALE = SaleCoverage(b"d" * 16, ORDER, KEY, D(1), D(1), D(1), True, False, True)


def facts():
    return dict(
        order_ref=ORDER,
        required=[CoverageLine(KEY, D(1))],
        remaining_demand=[],
        reserves=[],
        sales=[SALE],
        has_returns=False,
        snapshot_complete=True,
    )


def test_full_early_sale_without_reserve():
    assert evaluate_document_coverage(**facts()).covered


def test_partial_sale_plus_reserve_for_remaining_demand():
    data = facts()
    data.update(
        required=[CoverageLine(KEY, D(2))],
        remaining_demand=[CoverageLine(KEY, D(1))],
        reserves=[CoverageLine(KEY, D(1))],
    )
    assert evaluate_document_coverage(**data).covered


def test_stale_reserve_cannot_be_counted_after_sale():
    data = facts()
    data["reserves"] = [CoverageLine(KEY, D(1))]
    assert evaluate_document_coverage(**data).reason == "document_reserve_overlap"


@pytest.mark.parametrize(
    "change",
    [
        dict(posted=False),
        dict(marked=True),
        dict(origin_verified=False),
        dict(order_ref=b"x" * 16),
        dict(stock_outflow=D(0)),
        dict(order_outflow=D(0)),
        dict(quantity=D("NaN")),
        dict(stock_outflow=D("Infinity")),
        dict(quantity=D(-1)),
        dict(key=(b"x" * 16, bytes(16), bytes(16))),
    ],
)
def test_sale_without_full_evidence_denied(change):
    data = facts()
    data["sales"] = [replace(SALE, **change)]
    assert not evaluate_document_coverage(**data).covered


@pytest.mark.parametrize(
    "change",
    [
        dict(has_returns=True),
        dict(snapshot_complete=False),
        dict(sales=[]),
        dict(sales=[SALE, SALE]),
        dict(remaining_demand=[CoverageLine(KEY, D(1))]),
        dict(required=[CoverageLine(KEY, D(2))]),
        dict(reserves=[CoverageLine(KEY, D(-1))]),
    ],
)
def test_incomplete_or_ambiguous_facts_denied(change):
    data = facts()
    data.update(change)
    assert not evaluate_document_coverage(**data).covered


def test_split_documents_and_duplicate_order_lines():
    data = facts()
    data.update(
        required=[CoverageLine(KEY, D(1)), CoverageLine(KEY, D(1))],
        sales=[SALE, replace(SALE, document_ref=b"e" * 16)],
    )
    assert evaluate_document_coverage(**data).covered


def test_fractional_storage_units():
    data = facts()
    data.update(
        required=[CoverageLine(KEY, D("0.5"))],
        sales=[replace(SALE, quantity=D("0.5"), stock_outflow=D("0.5"), order_outflow=D("0.5"))],
    )
    assert evaluate_document_coverage(**data).covered
