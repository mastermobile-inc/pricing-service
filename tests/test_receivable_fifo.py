from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.models import ReceivableOpenDebtCache, ReceivableWorkItem
from app.schemas.receivable_workplace import ReceivableWorkplaceActionRequest
from app.services.receivable_canonical_debt_origin import (
    CANONICAL_DEBT_SELECTION_RULE,
    CANONICAL_DEBT_STATUS_DOCUMENT_TOTAL_BELOW_BALANCE,
    CANONICAL_DEBT_STATUS_MATCHED,
    CANONICAL_DEBT_STATUS_ORIGIN_BEFORE_HISTORY,
    CanonicalDebtSaleCandidate,
    resolve_canonical_debt_origin,
)
from app.services.receivable_workflow import (
    debt_age_days,
    stable_key_for_counterparty,
    sync_receivable_workflow,
)
from app.services.receivable_workplace import apply_receivable_workplace_action
from app.services.receivable_workplace_cache import (
    load_cached_open_debt_documents,
    ordered_open_debt_documents,
)
from app.services.receivables import CASE_BUYERS
from tests.test_receivable_workflow import FakeBitrixClient, _case, _settings


def _sale(document_ref: str, day: int, amount: str) -> CanonicalDebtSaleCandidate:
    return CanonicalDebtSaleCandidate(
        document_ref=document_ref,
        document_number=document_ref.upper(),
        document_date=datetime(2026, 8, day, 20, 30),
        gross_amount=Decimal(amount),
    )


def _dated_sale(
    document_ref: str,
    document_number: str,
    document_date: datetime,
    amount: str,
) -> CanonicalDebtSaleCandidate:
    return CanonicalDebtSaleCandidate(
        document_ref=document_ref,
        document_number=document_number,
        document_date=document_date,
        gross_amount=Decimal(amount),
    )


def test_task43_fifo_moves_origin_after_large_payment_for_rb030540() -> None:
    sales = [
        _dated_sale("closed-history", "СТАРЫЙ-МАССИВ", datetime(2026, 8, 15), "50000"),
        _dated_sale("anchor", "РБГУ0416825", datetime(2026, 8, 30, 15, 41, 18), "4390"),
        _dated_sale("sale-2", "РБГУ0417980", datetime(2026, 8, 31, 11, 25, 57), "330"),
        _dated_sale("sale-3", "РБГУ0417983", datetime(2026, 8, 31, 11, 26, 11), "1220"),
        _dated_sale("sale-4", "РБГУ0433901", datetime(2026, 9, 7, 13, 17, 13), "1440"),
        _dated_sale("sale-5", "РБГУ0445365", datetime(2026, 9, 12, 15, 11, 57), "1090"),
        _dated_sale("sale-6", "РБГУ0445714", datetime(2026, 9, 12, 16, 48, 52), "980"),
        _dated_sale("sale-7", "РБГУ0448113", datetime(2026, 9, 13, 18, 27, 15), "21990"),
        _dated_sale("sale-8", "РБГУ0453603", datetime(2026, 9, 16, 13, 30, 9), "7230"),
        _dated_sale("sale-9", "РБГУ0456353", datetime(2026, 9, 17, 15, 58, 3), "3620"),
        _dated_sale("sale-10", "РБГУ0456404", datetime(2026, 9, 17, 16, 13, 38), "1990"),
    ]
    resolution = resolve_canonical_debt_origin(
        opening_period=date(2026, 8, 1),
        opening_balance=Decimal("0"),
        daily_movements={
            date(2026, 8, 15): Decimal("50000"),
            date(2026, 8, 30): Decimal("4390"),
            date(2026, 8, 31): Decimal("1550"),
            date(2026, 9, 7): Decimal("-51122"),
            date(2026, 9, 12): Decimal("2070"),
            date(2026, 9, 13): Decimal("21990"),
            date(2026, 9, 16): Decimal("7230"),
            date(2026, 9, 17): Decimal("5610"),
        },
        sale_candidates=sales,
        current_balance=Decimal("41718"),
    )

    assert resolution.status == CANONICAL_DEBT_STATUS_MATCHED
    assert resolution.documents[0].document_number == "РБГУ0416825"
    assert resolution.documents[0].open_amount == Decimal("1828.00")
    assert all(document.document_number != "СТАРЫЙ-МАССИВ" for document in resolution.documents)
    assert sum(
        (document.open_amount for document in resolution.documents), Decimal("0")
    ) == Decimal("41718.00")


def test_task43_fifo_removes_year_old_origin_for_rb003879() -> None:
    resolution = resolve_canonical_debt_origin(
        opening_period=date(2025, 10, 1),
        opening_balance=Decimal("0"),
        daily_movements={
            date(2025, 10, 28): Decimal("26790"),
            date(2026, 9, 12): Decimal("-26810"),
            date(2026, 9, 13): Decimal("4280"),
            date(2026, 9, 14): Decimal("2590"),
            date(2026, 9, 15): Decimal("50"),
            date(2026, 9, 16): Decimal("50"),
            date(2026, 9, 17): Decimal("7680"),
        },
        sale_candidates=[
            _dated_sale("old", "СТАРЫЙ-ДОЛГ", datetime(2025, 10, 28), "26790"),
            _dated_sale("anchor", "РБГУ0446741", datetime(2026, 9, 13, 12, 12, 19), "610"),
            _dated_sale("sale-2", "РБГУ0447139", datetime(2026, 9, 13, 13, 55, 55), "1160"),
            _dated_sale("sale-3", "РБГУ0447227", datetime(2026, 9, 13, 14, 18, 22), "320"),
            _dated_sale("sale-4", "РБГУ0448041", datetime(2026, 9, 13, 18, 1, 48), "2190"),
            _dated_sale("sale-5", "РБГУ0449158", datetime(2026, 9, 14, 13, 23, 11), "2590"),
            _dated_sale("sale-6", "РБГУ0451561", datetime(2026, 9, 15, 14, 4, 58), "50"),
            _dated_sale("sale-7", "РБГУ0453974", datetime(2026, 9, 16, 15, 11, 22), "50"),
            _dated_sale("sale-8", "РБГУ0456208", datetime(2026, 9, 17, 15, 18, 42), "2190"),
            _dated_sale("sale-9", "РБГУ0456407", datetime(2026, 9, 17, 16, 14, 13), "5490"),
        ],
        current_balance=Decimal("14630"),
    )

    assert resolution.status == CANONICAL_DEBT_STATUS_MATCHED
    assert [document.document_number for document in resolution.documents][0] == "РБГУ0446741"
    assert resolution.documents[0].open_amount == Decimal("590.00")
    assert all(document.document_number != "СТАРЫЙ-ДОЛГ" for document in resolution.documents)
    assert (
        debt_age_days(
            [
                {"document_date": document.document_date, "open_amount": document.open_amount}
                for document in resolution.documents
            ],
            as_of=date(2026, 9, 17),
        )
        == 4
    )


@pytest.mark.parametrize(
    ("paid", "expected_documents", "expected_age"),
    [
        ("0", [("old", "200"), ("middle", "150"), ("new", "100")], 40),
        ("50", [("old", "150"), ("middle", "150"), ("new", "100")], 40),
        ("150", [("old", "50"), ("middle", "150"), ("new", "100")], 40),
        ("200", [("middle", "150"), ("new", "100")], 31),
        ("225", [("middle", "125"), ("new", "100")], 31),
        ("349.99", [("middle", "0.01"), ("new", "100")], 31),
        ("350", [("new", "100")], 21),
        ("450", [], None),
        ("500", [], None),
    ],
)
def test_fifo_payments_and_age_share_the_same_open_documents(
    paid: str, expected_documents: list[tuple[str, str]], expected_age: int | None
) -> None:
    balance = Decimal("450") - Decimal(paid)
    resolution = resolve_canonical_debt_origin(
        opening_period=date(2026, 8, 1),
        opening_balance=Decimal("0"),
        daily_movements={
            date(2026, 8, 1): Decimal("200"),
            date(2026, 8, 10): Decimal("150"),
            date(2026, 8, 20): Decimal("100"),
            date(2026, 9, 1): -Decimal(paid),
        },
        sale_candidates=[
            _sale("new", 20, "100"),
            _sale("old", 1, "200"),
            _sale("middle", 10, "150"),
        ],
        current_balance=balance,
    )

    assert resolution.status == CANONICAL_DEBT_STATUS_MATCHED
    assert [(document.document_ref, document.open_amount) for document in resolution.documents] == [
        (document_ref, Decimal(amount)) for document_ref, amount in expected_documents
    ]
    assert sum((document.open_amount for document in resolution.documents), Decimal("0")) == max(
        balance, Decimal("0")
    )
    for document in resolution.documents:
        assert document.open_amount == document.gross_amount + document.closing_amount
        assert document.closing_amount <= 0
    documents = [
        {"document_date": document.document_date, "open_amount": document.open_amount}
        for document in resolution.documents
    ]
    assert debt_age_days(documents, as_of=date(2026, 9, 10)) == expected_age


def test_fifo_carries_advance_to_next_debt_cycle_and_uses_stable_ties() -> None:
    resolution = resolve_canonical_debt_origin(
        opening_period=date(2026, 8, 1),
        opening_balance=Decimal("-50"),
        daily_movements={date(2026, 8, 10): Decimal("200")},
        sale_candidates=[_sale("sale-b", 10, "100"), _sale("sale-a", 10, "100")],
        current_balance=Decimal("150"),
    )

    assert [(document.document_ref, document.open_amount) for document in resolution.documents] == [
        ("sale-a", Decimal("50")),
        ("sale-b", Decimal("100")),
    ]


def test_fifo_distributes_successive_payments_between_sales() -> None:
    resolution = resolve_canonical_debt_origin(
        opening_period=date(2026, 8, 1),
        opening_balance=Decimal("0"),
        daily_movements={
            date(2026, 8, 1): Decimal("100"),
            date(2026, 8, 2): Decimal("-60"),
            date(2026, 8, 3): Decimal("100"),
            date(2026, 8, 4): Decimal("-80"),
        },
        sale_candidates=[_sale("old", 1, "100"), _sale("new", 3, "100")],
        current_balance=Decimal("60"),
    )

    assert resolution.status == CANONICAL_DEBT_STATUS_MATCHED
    assert [(document.document_ref, document.open_amount) for document in resolution.documents] == [
        ("new", Decimal("60")),
    ]
    assert resolution.documents[0].closing_amount == Decimal("-40")


def test_fifo_excludes_documents_from_a_fully_closed_previous_cycle() -> None:
    resolution = resolve_canonical_debt_origin(
        opening_period=date(2026, 8, 1),
        opening_balance=Decimal("0"),
        daily_movements={
            date(2026, 8, 1): Decimal("100"),
            date(2026, 8, 2): Decimal("-100"),
            date(2026, 8, 3): Decimal("50"),
        },
        sale_candidates=[_sale("closed", 1, "100"), _sale("open", 3, "50")],
        current_balance=Decimal("50"),
    )

    assert [(document.document_ref, document.open_amount) for document in resolution.documents] == [
        ("open", Decimal("50")),
    ]


@pytest.mark.parametrize(
    ("opening", "movement", "balance", "expected_status"),
    [
        ("10", "100", "110", CANONICAL_DEBT_STATUS_ORIGIN_BEFORE_HISTORY),
        ("0", "200", "200", CANONICAL_DEBT_STATUS_DOCUMENT_TOTAL_BELOW_BALANCE),
    ],
)
def test_fifo_does_not_invent_documents_for_incomplete_history(
    opening: str, movement: str, balance: str, expected_status: str
) -> None:
    resolution = resolve_canonical_debt_origin(
        opening_period=date(2026, 8, 1),
        opening_balance=Decimal(opening),
        daily_movements={date(2026, 8, 10): Decimal(movement)},
        sale_candidates=[_sale("sale", 10, "100")],
        current_balance=Decimal(balance),
    )

    assert resolution.status == expected_status
    assert resolution.documents == ()


def test_open_document_table_filters_closed_rows_and_sorts_oldest_first() -> None:
    documents = [
        {"document_ref": "new", "document_date": "2026-08-20", "open_amount": "100"},
        {"document_ref": "closed", "document_date": "2026-08-01", "open_amount": "0"},
        {"document_ref": "old", "document_date": "2026-08-10", "open_amount": "0.01"},
        {"document_ref": "credit", "document_date": "2026-08-01", "open_amount": "-50"},
    ]

    assert [document["document_ref"] for document in ordered_open_debt_documents(documents)] == [
        "old",
        "new",
    ]
    assert len(documents) == 4


@pytest.mark.parametrize("document_date", [None, "broken-date", ""])
def test_missing_date_in_any_open_document_makes_age_unknown(document_date: str | None) -> None:
    documents = [
        {"document_date": "2026-08-10", "open_amount": "100"},
        {"document_date": document_date, "open_amount": "50"},
    ]

    assert debt_age_days(documents, as_of=date(2026, 9, 10)) is None


@pytest.mark.parametrize(
    "source_rule", ["onec_canonical_continuous_balance_origin", CANONICAL_DEBT_SELECTION_RULE]
)
def test_cache_requires_rebuild_before_old_allocation_can_be_used(
    db_session: Session, source_rule: str
) -> None:
    db_session.add(
        ReceivableOpenDebtCache(
            snapshot_date=date(2026, 9, 10),
            counterparty_ref="cp-fifo",
            source_status="ready",
            documents=[
                {
                    "document_date": "2026-08-10",
                    "open_amount": "100",
                    "statement_selection_rule": source_rule,
                }
            ],
        )
    )
    db_session.flush()

    cache = load_cached_open_debt_documents(
        db_session, snapshot_date=date(2026, 9, 10), counterparty_refs=["cp-fifo"]
    )

    if source_rule == CANONICAL_DEBT_SELECTION_RULE:
        assert len(cache.documents_by_counterparty["cp-fifo"]) == 1
        assert not cache.hidden_counterparty_refs
    else:
        assert cache.documents_by_counterparty["cp-fifo"] == []
        assert cache.hidden_counterparty_refs == frozenset({"cp-fifo"})
        assert not cache.document_mismatch_counterparty_refs
        assert cache.outdated_counterparty_refs == frozenset({"cp-fifo"})
        assert cache.source_status == "cache_outdated"


def test_workplace_action_keeps_age_from_the_same_fifo_table(db_session: Session) -> None:
    as_of = date(2026, 9, 10)
    db_session.add(_case(snapshot_date=as_of, segment=CASE_BUYERS, balance=Decimal("150")))
    db_session.add(
        ReceivableOpenDebtCache(
            snapshot_date=as_of,
            counterparty_ref="cp-a",
            source_status="ready",
            documents=[
                {
                    "document_ref": "new",
                    "document_date": "2026-09-01T12:00:00",
                    "open_amount": "100",
                    "statement_selection_rule": CANONICAL_DEBT_SELECTION_RULE,
                },
                {
                    "document_ref": "old",
                    "document_date": "2026-08-01T20:00:00",
                    "open_amount": "50",
                    "statement_selection_rule": CANONICAL_DEBT_SELECTION_RULE,
                },
            ],
        )
    )
    db_session.flush()

    response = apply_receivable_workplace_action(
        db_session,
        snapshot_date=as_of,
        counterparty_ref="cp-a",
        payload=ReceivableWorkplaceActionRequest(status="waiting_payment"),
    )

    assert response is not None
    item = db_session.query(ReceivableWorkItem).one()
    assert item.age_days == 40
    assert [document["document_ref"] for document in item.chain_documents] == ["old", "new"]
    assert item.current_balance == Decimal("150")
    assert item.status == "waiting_payment"


@pytest.mark.parametrize("counterparty_ref", ["cp-a", "CP-A"])
def test_old_cache_does_not_erase_card_fields_status_or_close_the_card(
    db_session: Session, counterparty_ref: str
) -> None:
    as_of = date(2026, 9, 10)
    documents = [
        {
            "document_ref": "legacy",
            "document_date": "2025-11-18",
            "open_amount": "150",
            "statement_selection_rule": "onec_canonical_continuous_balance_origin",
        }
    ]
    db_session.add(
        _case(
            snapshot_date=as_of,
            segment=CASE_BUYERS,
            counterparty_ref=counterparty_ref,
            balance=Decimal("150"),
        )
    )
    db_session.add(
        ReceivableOpenDebtCache(
            snapshot_date=as_of,
            counterparty_ref=counterparty_ref,
            source_status="ready",
            documents=documents,
        )
    )
    item = ReceivableWorkItem(
        stable_key=stable_key_for_counterparty(counterparty_ref),
        counterparty_ref=counterparty_ref,
        status="promised_payment",
        current_balance=Decimal("150"),
        age_days=24,
        chain_documents=documents,
        bitrix_item_id=99,
    )
    db_session.add(item)
    db_session.flush()
    bitrix = FakeBitrixClient()

    summary = sync_receivable_workflow(
        db_session,
        as_of=as_of,
        settings=_settings(),
        bitrix_client=bitrix,
        sync_sms=False,
        allow_closure=True,
    )

    assert summary.data_quality_skipped == 1
    assert summary.errors
    assert summary.work_items_closed == 0
    assert not bitrix.updated and not bitrix.added
    assert item.status == "promised_payment"
    assert item.current_balance == Decimal("150")
    assert item.age_days == 24
    assert item.chain_documents == documents


def test_workplace_without_cache_does_not_display_historical_chain_as_open(
    db_session: Session,
) -> None:
    as_of = date(2026, 9, 10)
    case = _case(snapshot_date=as_of, segment=CASE_BUYERS, balance=Decimal("150"))
    assert case.chain_documents
    db_session.add(case)
    db_session.flush()

    response = apply_receivable_workplace_action(
        db_session,
        snapshot_date=as_of,
        counterparty_ref="cp-a",
        payload=ReceivableWorkplaceActionRequest(status="waiting_payment"),
    )

    assert response is not None
    assert response.item.documents == []
    item = db_session.query(ReceivableWorkItem).one()
    assert item.age_days is None
    assert item.chain_documents == []
