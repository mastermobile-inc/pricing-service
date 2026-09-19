from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.schemas.procurement_pricing import PricingFilter, PricingRow, PricingTable
from app.services import procurement_pricing_source as source


def test_page_reads_only_selected_prices_but_price_sort_reads_all():
    source._snapshot_cache.clear()
    rows = [PricingRow(code=str(i), name=str(i)) for i in range(5)]
    snapshot = PricingTable(
        items=rows,
        total=5,
        start=date(2026, 9, 1),
        end=date(2026, 9, 30),
        observed_at=datetime.now(UTC),
        facts_through=date(2026, 9, 19),
    )
    db = MagicMock()

    def prices(codes):
        return {
            (code, "bronze"): {"price": Decimal(5 - int(code)), "currency": "RUB"} for code in codes
        }

    with (
        patch.object(source, "_load_table", return_value=snapshot) as load,
        patch.object(source, "current_prices", side_effect=prices) as read,
    ):
        filters = PricingFilter(start=snapshot.start, end=snapshot.end, offset=1, limit=2)
        result = source.build_table(db, filters)
        assert result.total == 5
        assert [r.code for r in result.items] == ["1", "2"]
        assert read.call_args.args[0] == ["1", "2"]
        result = source.build_table(db, filters.model_copy(update={"sort": "bronze"}))
        assert [r.code for r in result.items] == ["3", "2"]
        assert len(read.call_args.args[0]) == 5
        assert load.call_count == 1
        assert all(row.bronze is None for row in snapshot.items)
    source._snapshot_cache.clear()


def test_period_refund_cost_and_confirmed_defects_have_separate_bases():
    db = MagicMock()
    product = SimpleNamespace(
        id=1,
        code_1c="ABC",
        name="Test",
        subject_1c=None,
        subject=None,
        brand=None,
        quality_raw=None,
        quality=None,
    )
    db.scalars.return_value.all.return_value = [product]
    db.execute.return_value = []
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value.execute.return_value.scalar.return_value = (
        datetime(2020, 1, 1)
    )
    sales = {"ABC": {"sales_qty": Decimal(10), "sales_amount": Decimal(1000)}}
    returns = {"ABC": {"return_amount": Decimal(200), "defect_return_qty": Decimal(2)}}
    costs = {"ABC": {"cost_amount": Decimal(600)}}
    with (
        patch.object(source, "get_onec_engine", return_value=engine),
        patch.object(source, "_period", return_value=(sales, returns, costs)),
    ):
        row = source._load_table(
            db, PricingFilter(start=date(2025, 9, 1), end=date(2025, 9, 30)), include_prices=False
        ).items[0]
    assert row.sales_amount == 800
    assert row.profitability == 25
    assert row.defect_pct == 20
    assert row.defect_qty == 2
    assert row.sales_qty == 10
    assert row.forecast_qty is None
    assert row.forecast_status == "completed"
    # An old first sale alone does not prove uninterrupted history.
    with (
        patch.object(source, "get_onec_engine", return_value=engine),
        patch.object(source, "_period", return_value=(sales, returns, costs)),
        patch.object(
            source,
            "get_settings",
            return_value=SimpleNamespace(procurement_pricing_history_complete_from=None),
        ),
    ):
        today = date.today()
        row = source._load_table(
            db,
            PricingFilter(start=today.replace(day=1), end=today + timedelta(days=30)),
            include_prices=False,
        ).items[0]
    assert row.forecast_qty is None
    assert row.forecast_status == "history_incomplete"


def test_period_sums_line_amount_instead_of_unit_price():
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE _Document203 (_IDRRef INTEGER, _Marked INTEGER, "
                "_Posted INTEGER, _Date_Time TEXT)"
            )
        )
        connection.execute(text("CREATE TABLE _Reference62 (_IDRRef INTEGER, _Code TEXT)"))
        connection.execute(
            text(
                "CREATE TABLE _Document203_VT4966 "
                "(_Document203_IDRRef INTEGER, _Fld4974RRef INTEGER, "
                "_Fld4971 NUMERIC, _Fld4979 NUMERIC, _Fld4982 NUMERIC)"
            )
        )
        connection.execute(text("INSERT INTO _Document203 VALUES (1,0,1,'2026-08-12')"))
        connection.execute(text("INSERT INTO _Reference62 VALUES (1,'SKU')"))
        # Two units at 100 with a discount: use the recorded 180, not 100 or 200.
        connection.execute(text("INSERT INTO _Document203_VT4966 VALUES (1,1,2,180,100)"))
        fake = MagicMock()
        fake.connect.return_value.__enter__.return_value.execute.side_effect = (
            lambda statement, params: (
                connection.execute(statement, params)
                if "FROM _Document203 sale" in str(statement)
                else MagicMock(mappings=lambda: [])
            )
        )
        with patch.object(source, "get_onec_engine", return_value=fake):
            sales, _, _ = source._period([], date(2026, 8, 1), date(2026, 9, 1))
        assert sales["SKU"]["sales_qty"] == 2
        assert sales["SKU"]["sales_amount"] == 180
    engine.dispose()
