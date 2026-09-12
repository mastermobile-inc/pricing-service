from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, text

from app.services.assortment_lifecycle_facts import (
    DocumentLineMapping,
    _chunks,
    _fetch_first_document_event_rows,
    _folder_like_patterns,
    build_assortment_lifecycle_fact_records,
    drop_cancelled_order_lines,
    enrich_nomenclature_rows_with_product_snapshot,
    fetch_order_correction_deltas,
    validate_document_line_mapping,
    validate_warehouse_policy,
)


def _warehouse_policy() -> list[dict[str, object]]:
    return validate_warehouse_policy(
        {
            "warehouses": [
                {"warehouse_code": "shop-1", "sells_systematically": True},
                {"warehouse_code": "central", "is_central": True},
                {"warehouse_code": "defect", "is_defect_warehouse": True},
                {"warehouse_code": "transit", "is_transit": True},
                {"warehouse_code": "rare", "is_non_systematic_sale": True},
            ]
        }
    )


def test_build_facts_from_rows_builds_cargo_receipts_and_overlays() -> None:
    facts, summary = build_assortment_lifecycle_fact_records(
        nomenclature_rows=[
            {
                "nomenclature_ref": "0xA",
                "nomenclature_code": "РБ0001",
                "name": "Дисплей тестовый A",
                "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
                "short_name_1c": "Дисп. тест A (ORIG)",
                "additional_name_1c": "Display test A (ORIG)",
                "vendor_sku_1c": "OEM-DSP-TEST-BLK-OR",
                "article": "SKU-001",
                "subject_1c": "Дисплей",
                "tag": "iPhone, рамка",
                "quality_raw": "ORIG100",
                "brand_compatibility": "Apple",
                "model_compatibility": "iPhone 13",
                "characteristic_values": {"frame": "yes", "ic_pad": "yes"},
                "created_at": "2025-12-20",
                "item_value": "300",
            },
            {
                "nomenclature_ref": "0xB",
                "nomenclature_code": "РБ0002",
                "name": "Дисплей тестовый B",
                "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
                "item_value": "100",
            },
        ],
        supplier_order_rows=[
            {
                "nomenclature_ref": "0xA",
                "order_date": "2024-01-01",
                "cargo_handoff_date": "2024-01-05",
                "historical_aggregate": True,
            },
            {
                "nomenclature_ref": "0xA",
                "order_date": "2026-01-01",
                "cargo_handoff_date": "2026-01-05",
                "line_price": "300",
            },
            {
                "nomenclature_ref": "0xA",
                "order_date": "2026-02-01",
                "cargo_handoff_date": "2026-02-05",
                "line_price": "320",
            },
        ],
        receipt_rows=[
            {"nomenclature_ref": "0xA", "receipt_date": "2026-01-10"},
            {"nomenclature_ref": "0xA", "receipt_date": "2026-02-10"},
            {"nomenclature_ref": "0xA", "receipt_date": "2026-03-10"},
            {"nomenclature_ref": "0xA", "receipt_date": "2026-04-10"},
            {"nomenclature_ref": "0xA", "receipt_date": "2026-05-10"},
        ],
        warehouse_policy=_warehouse_policy(),
        manual_overrides={
            "РБ0001": {
                "working_confirmed_by_folder_responsible": True,
                "analog_winner_confirmed_by_folder_responsible": True,
                "manual_expensive_profile": "fast_expensive",
            }
        },
        manager_signals={
            "РБ0001": [
                {
                    "manager_id": "manager-1",
                    "quantity": 1,
                    "source": "offline_call",
                    "signal_date": "2026-01-03",
                    "comment": "Клиент спрашивал",
                }
            ]
        },
        history_start=date(2025, 12, 1),
    )

    first = facts[0]
    assert summary["items"] == 2
    assert first["nomenclature_code"] == "РБ0001"
    assert first["card_created_at"] == "2025-12-20"
    assert first["short_name_1c"] == "Дисп. тест A (ORIG)"
    assert first["additional_name_1c"] == "Display test A (ORIG)"
    assert first["vendor_sku_1c"] == "OEM-DSP-TEST-BLK-OR"
    assert first["first_supplier_order_at"] == "2024-01-01"
    assert first["historical_first_cargo_handoff_at"] == "2024-01-05"
    assert first["supplier_order_cargo_handoff_dates"] == ["2026-01-05", "2026-02-05"]
    assert first["receipt_dates"] == [
        "2026-01-10",
        "2026-02-10",
        "2026-03-10",
        "2026-04-10",
        "2026-05-10",
    ]
    assert first["has_need_signal"] is True
    assert first["manager_need_signals"][0]["manager_id"] == "manager-1"
    assert first["expensive_item_value"] == "300"
    assert first["expensive_group_values"] == ["300", "100"]
    assert first["expensive_route_days"] == 5
    assert first["working_confirmed_by_folder_responsible"] is True
    assert first["analog_winner_confirmed_by_folder_responsible"] is True
    assert first["manual_expensive_profile"] == "fast_expensive"
    assert first["feature_snapshot_schema"] == "procurement_feature_snapshot.v1"
    assert first["article"] == "SKU-001"
    assert first["subject_1c"] == "Дисплей"
    assert first["item_tags"] == ["iPhone", "рамка"]
    assert first["quality_normalized"] == "original"
    assert first["characteristic_values"] == {"frame": "yes", "ic_pad": "yes"}
    assert first["price_segment"] == "mid_high"
    assert first["missing_required_attributes"] == []
    assert first["data_quality_score"] == "1.00"
    assert first["future_ka_mapping_status"] == "ready"
    assert first["calculation_unit_level"] == "subject_tag"
    assert first["calculation_unit_source"] == "1c_properties"
    assert first["demand_method_code"] == "store_need"
    assert [warehouse["warehouse_code"] for warehouse in first["warehouses"]] == [
        "shop-1",
        "central",
        "defect",
        "transit",
        "rare",
    ]


def test_cancelled_order_lines_are_dropped_and_partial_shipment_survives() -> None:
    # Синтетический пример: корректировка снимает всю позицию первой партии,
    # второй заказ живой, а для другого товара сохраняется частичная отправка.
    rows = [
        {
            "document_ref": "cancelled-order",
            "nomenclature_ref": "test-item",
            "line_quantity": "10",
            "cargo_handoff_date": "2026-08-28",
        },
        {
            "document_ref": "live-order",
            "nomenclature_ref": "test-item",
            "line_quantity": "10",
            "cargo_handoff_date": None,
        },
        {
            "document_ref": "partial-order",
            "nomenclature_ref": "item-battery",
            "line_quantity": "100",
            "cargo_handoff_date": "2026-09-01",
        },
    ]
    deltas = {
        ("cancelled-order", "test-item"): Decimal("-10"),
        ("partial-order", "item-battery"): Decimal("-1"),
    }

    kept = drop_cancelled_order_lines(rows, deltas)

    kept_orders = {row["document_ref"] for row in kept}
    # Снятая подчистую позиция не даёт ни заказа, ни сдачи в cargo.
    assert "cancelled-order" not in kept_orders
    # Живой заказ остаётся.
    assert "live-order" in kept_orders
    # Частичное снятие не отменяет отправку: заказано 100, снято 1, едет 99.
    assert "partial-order" in kept_orders


def test_order_lines_survive_when_corrections_are_absent() -> None:
    rows = [{"document_ref": "zakaz-1", "nomenclature_ref": "item-1", "line_quantity": "5"}]

    assert len(drop_cancelled_order_lines(rows, {})) == 1


@pytest.mark.parametrize(
    "delta, survives", [("-10", False), ("-11", False), ("-7", True), ("2", True)]
)
def test_corrections_sum_duplicate_lines_once_per_order_and_item(delta, survives) -> None:
    rows = [
        {"document_ref": "order", "nomenclature_ref": "item", "line_quantity": qty}
        for qty in ("4", "6")
    ]
    other = {"document_ref": "order", "nomenclature_ref": "other", "line_quantity": "1"}
    another_order = {"document_ref": "next", "nomenclature_ref": "item", "line_quantity": "1"}

    kept = drop_cancelled_order_lines(
        [*rows, other, another_order], {("order", "item"): Decimal(delta)}
    )

    assert kept == ([*rows, other, another_order] if survives else [other, another_order])


def test_zero_quantity_without_corrections_is_not_an_order_event() -> None:
    rows = [{"document_ref": "order", "nomenclature_ref": "item", "line_quantity": "0"}]
    assert drop_cancelled_order_lines(rows, {}) == []


def test_legacy_rows_without_quantity_keep_backward_compatibility() -> None:
    rows = [{"document_ref": "order", "nomenclature_ref": "item"}]
    assert drop_cancelled_order_lines(rows, {}) == rows


def _correction_test_mapping() -> DocumentLineMapping:
    return DocumentLineMapping(
        document_table="orders",
        line_table="order_lines",
        line_document_column="order_ref",
        line_nomenclature_column="item_ref",
        line_quantity_column="quantity",
        cargo_handoff_column="cargo",
        correction_document_table="corrections",
        correction_line_table="correction_lines",
        correction_order_column="order_ref",
        correction_line_document_column="correction_ref",
        correction_nomenclature_column="item_ref",
        correction_quantity_column="quantity",
    )


@pytest.fixture
def correction_sql_engine():
    """Execute production SELECTs locally; translate only SQL Server syntax.

    Joins, filters, grouping and HAVING are executed by SQLite, not mocked.
    Reference IDs are text and the posted/marked flags are integers in this fixture.
    """
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        for query in (
            "CREATE TABLE orders (_IDRRef TEXT, _Date_Time TEXT, cargo TEXT, _Posted INT, _Marked INT)",
            "CREATE TABLE order_lines (order_ref TEXT, item_ref TEXT, quantity NUMERIC)",
            "CREATE TABLE corrections (_IDRRef TEXT, order_ref TEXT, _Posted INT, _Marked INT)",
            "CREATE TABLE correction_lines (correction_ref TEXT, item_ref TEXT, quantity NUMERIC)",
            "INSERT INTO orders VALUES ('cancelled', '2024-01-01', '2024-01-05', 1, 0),"
            " ('next', '2026-09-01', NULL, 1, 0),"
            " ('partial', '2025-01-01', '2025-01-05', 1, 0),"
            " ('draft', '2023-01-01', '2023-01-05', 0, 0),"
            " ('deleted', '2023-02-01', '2023-02-05', 1, 1)",
            "INSERT INTO order_lines VALUES ('cancelled', 'item', 4), ('cancelled', 'item', 6),"
            " ('cancelled', 'gone', 10), ('next', 'item', 10),"
            " ('partial', 'partial-item', 4), ('partial', 'partial-item', 6),"
            " ('draft', 'item', 100), ('deleted', 'item', 100)",
            "INSERT INTO corrections VALUES ('c1', 'cancelled', 1, 0),"
            " ('c2', 'cancelled', 1, 0), ('c3', 'partial', 1, 0),"
            " ('draft-c', 'next', 0, 0), ('deleted-c', 'next', 1, 1),"
            " ('add', 'next', 1, 0)",
            "INSERT INTO correction_lines VALUES ('c1', 'item', -7), ('c2', 'item', -3),"
            " ('c1', 'gone', -11), ('c3', 'partial-item', -7),"
            " ('draft-c', 'item', -100), ('deleted-c', 'item', -100), ('add', 'item', 2)",
        ):
            conn.execute(text(query))

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def translate_sqlserver_syntax(conn, cursor, statement, parameters, context, executemany):
        statement = re.sub(r"CONVERT\(varchar\(34\), ([\w.\[\]]+), 1\)", r"\1", statement)
        statement = statement.replace("CONVERT(datetime, '17530101', 112)", "'1753-01-01'")
        statement = statement.replace("dbo.", "").replace(" WITH (NOLOCK)", "")
        return statement.replace("ISNULL(", "IFNULL("), parameters

    yield engine
    engine.dispose()


def test_correction_query_sums_only_posted_not_deleted_documents(correction_sql_engine) -> None:
    deltas = fetch_order_correction_deltas(
        correction_sql_engine, _correction_test_mapping(), allowed_refs={"item", "partial-item"}
    )
    assert deltas == {
        ("cancelled", "item"): Decimal("-10"),
        ("next", "item"): Decimal("2"),
        ("partial", "partial-item"): Decimal("-7"),
    }


def test_historical_aggregate_excludes_cancelled_orders_and_keeps_partial(correction_sql_engine):
    rows = _fetch_first_document_event_rows(
        correction_sql_engine,
        _correction_test_mapping(),
        allowed_refs={"item", "partial-item", "gone"},
    )
    by_ref = {row["nomenclature_ref"]: row for row in rows}
    assert set(by_ref) == {"item", "partial-item"}
    assert by_ref["item"]["order_date"] == "2026-09-01"
    assert by_ref["item"]["cargo_handoff_date"] is None
    assert by_ref["partial-item"]["order_date"] == "2025-01-01"
    assert by_ref["partial-item"]["cargo_handoff_date"] == "2025-01-05"


def test_correction_mapping_validates_existing_columns(correction_sql_engine) -> None:
    assert validate_document_line_mapping(correction_sql_engine, _correction_test_mapping()) == ()
    bad = replace(_correction_test_mapping(), correction_quantity_column="missing_quantity")
    assert "column_missing:correction_lines.missing_quantity" in validate_document_line_mapping(
        correction_sql_engine, bad
    )


@pytest.mark.parametrize(
    "field",
    [
        "line_quantity_column",
        "correction_document_table",
        "correction_line_table",
        "correction_order_column",
        "correction_line_document_column",
        "correction_nomenclature_column",
        "correction_quantity_column",
    ],
)
def test_partial_correction_mapping_blocks_classification(correction_sql_engine, field) -> None:
    mapping = replace(_correction_test_mapping(), **{field: ""})
    assert f"correction_mapping_missing:{field}" in validate_document_line_mapping(
        correction_sql_engine, mapping
    )


def test_build_facts_excludes_bitok_before_events_and_summary() -> None:
    facts, summary = build_assortment_lifecycle_fact_records(
        nomenclature_rows=[
            {
                "nomenclature_ref": "0xA",
                "nomenclature_code": "KEEP",
                "name": "Дисплей обычный",
                "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
            },
            {
                "nomenclature_ref": "0xB",
                "nomenclature_code": "DROP",
                "name": "Дисплей (БИТОК)",
                "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
            },
        ],
        supplier_order_rows=[{"nomenclature_ref": "0xB", "order_date": "2026-08-01"}],
        receipt_rows=[{"nomenclature_ref": "0xB", "receipt_date": "2026-08-10"}],
        warehouse_policy=_warehouse_policy(),
    )

    assert [fact["nomenclature_code"] for fact in facts] == ["KEEP"]
    assert summary["scope_policy"]["excluded_reason_counts"] == {"excluded_display_name_bitok": 1}


def test_build_facts_marks_history_truncated_when_first_event_hits_boundary() -> None:
    facts, summary = build_assortment_lifecycle_fact_records(
        nomenclature_rows=[
            {
                "nomenclature_ref": "0xA",
                "nomenclature_code": "РБ0001",
                "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
            }
        ],
        supplier_order_rows=[
            {
                "nomenclature_ref": "0xA",
                "order_date": "2025-01-01",
                "cargo_handoff_date": "2025-01-05",
            }
        ],
        receipt_rows=[],
        warehouse_policy=_warehouse_policy(),
        history_start=date(2025, 1, 1),
    )

    assert facts[0]["warnings"] == ["history_truncated"]
    assert summary["warnings"] == {"history_truncated": 1}


def test_manual_override_maps_legacy_exclusive_status_to_commercial_mark() -> None:
    facts, _ = build_assortment_lifecycle_fact_records(
        nomenclature_rows=[
            {
                "nomenclature_ref": "0xA",
                "nomenclature_code": "РБ0001",
                "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
            }
        ],
        supplier_order_rows=[],
        receipt_rows=[],
        warehouse_policy=_warehouse_policy(),
        manual_overrides={
            "РБ0001": {
                "manual_status": "exclusive",
                "manual_reason": "Товар есть только у нас",
                "manual_approved_by": "Омар",
                "manual_changed_at": "2026-06-27",
            }
        },
    )

    assert "manual_status" not in facts[0]
    assert facts[0]["commercial_marks"] == ["exclusive"]
    assert facts[0]["exclusive_reason"] == "Товар есть только у нас"
    assert facts[0]["exclusive_approved_by"] == "Омар"
    assert facts[0]["exclusive_checked_at"] == "2026-06-27"


def test_manual_do_not_order_override_blocks_demand_formula() -> None:
    facts, _ = build_assortment_lifecycle_fact_records(
        nomenclature_rows=[
            {
                "nomenclature_ref": "0xA",
                "nomenclature_code": "РБ0001",
                "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
            }
        ],
        supplier_order_rows=[],
        receipt_rows=[],
        warehouse_policy=_warehouse_policy(),
        manual_overrides={
            "РБ0001": {
                "manual_status": "do_not_order",
                "manual_reason": "Родился мертвым",
                "manual_approved_by": "chat",
                "manual_changed_at": "2026-07-03",
            }
        },
    )

    assert facts[0]["manual_status"] == "do_not_order"
    assert facts[0]["demand_method_code"] == "manual_review"
    assert facts[0]["demand_method_confidence"] == "0.00"
    assert (
        facts[0]["demand_method_reason"]
        == "Есть ручной стоп или статус, обычную формулу не применяем."
    )


def test_feature_snapshot_infers_display_subject_and_model_without_quality() -> None:
    facts, _ = build_assortment_lifecycle_fact_records(
        nomenclature_rows=[
            {
                "nomenclature_ref": "0xA",
                "nomenclature_code": "РБ000030751",
                "name": (
                    "Дисплей для LeEco Le 2 (X520/X526/X527) / Le 2 (X620) "
                    "(в сборе с тачскрином) (розовый)"
                ),
                "folder_path": "ОБЩИЙ КАТАЛОГ / Дисплеи для LeEco",
            }
        ],
        supplier_order_rows=[],
        receipt_rows=[],
        warehouse_policy=_warehouse_policy(),
    )

    assert facts[0]["subject_1c"] == "дисплей"
    assert facts[0]["brand_compatibility"] == "LeEco"
    assert facts[0]["model_compatibility"] == "LeEco Le 2 (X520/X526/X527) / Le 2 (X620)"
    assert facts[0]["missing_required_attributes"] == ["quality_raw"]
    assert facts[0]["data_quality_score"] == "0.67"
    assert facts[0]["future_ka_mapping_status"] == "needs_mapping"
    assert facts[0]["calculation_unit_level"] == "property_group"


def test_matrix_folder_is_treated_as_display_scope() -> None:
    facts, _ = build_assortment_lifecycle_fact_records(
        nomenclature_rows=[
            {
                "nomenclature_ref": "0xA",
                "nomenclature_code": "РБ000042811",
                "name": "Матрица для Lenovo ThinkPad T480 14.0 Slim 30 pin",
                "folder_path": "ОБЩИЙ КАТАЛОГ / Запчасти для ноутбуков / Матрицы",
            }
        ],
        supplier_order_rows=[],
        receipt_rows=[],
        warehouse_policy=_warehouse_policy(),
    )

    assert facts[0]["subject_1c"] == "дисплей"
    assert facts[0]["model_compatibility"] == "Lenovo ThinkPad T480 14.0 Slim 30 pin"
    assert facts[0]["brand_compatibility"] == "Lenovo"
    assert facts[0]["missing_required_attributes"] == ["quality_raw"]
    assert _folder_like_patterns("дисплеи") == ("%дисплеи%", "%Матриц%")


def test_enrich_nomenclature_rows_with_product_snapshot_adds_product_attributes() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE product ("
                "id INTEGER PRIMARY KEY, article TEXT, fact_sku TEXT, code_1c TEXT, "
                "info_system_code TEXT, name TEXT, brand TEXT, category TEXT, "
                "subject TEXT, subject_1c TEXT, vid_nomenklatury_1c TEXT, "
                "quality_raw TEXT, display_quality_raw TEXT, quality TEXT, display_quality TEXT, "
                "display_type TEXT, display_construction TEXT, display_refresh_rate_hz INTEGER, "
                "display_screen_kit TEXT, display_has_frame BOOLEAN, display_has_touch BOOLEAN, "
                "display_has_ic_pad BOOLEAN, display_has_binding_no_solder BOOLEAN, "
                "display_backlight TEXT, display_matrix_tags TEXT, display_diagonal TEXT, "
                "display_resolution TEXT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE productcompatibility (" "product_id INTEGER, value TEXT, source TEXT)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO product "
                "(id, article, fact_sku, code_1c, info_system_code, name, category, "
                "subject_1c, quality_raw, quality, display_quality, display_type, "
                "display_has_frame) "
                "VALUES "
                "(1, '022904', 'OEM-DSP-IPD34-OR', 'РБ000006737', 'abc', "
                "'Дисплей тестовый', 'Дисплеи для планшетов', 'дисплей', "
                "'ORIG', 'Original', 'Original', 'In-Cell', 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO productcompatibility (product_id, value, source) "
                "VALUES (1, 'Apple iPad 3', 'onec'), (1, 'Apple iPad 4', 'onec')"
            )
        )

    enriched = enrich_nomenclature_rows_with_product_snapshot(
        engine,
        [
            {
                "nomenclature_ref": "0xA",
                "nomenclature_code": "РБ000006737",
                "name": "Дисплей тестовый",
                "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
                "item_value": "300",
            }
        ],
    )
    facts, _ = build_assortment_lifecycle_fact_records(
        nomenclature_rows=enriched,
        supplier_order_rows=[],
        receipt_rows=[],
        warehouse_policy=_warehouse_policy(),
    )

    assert enriched[0]["subject_1c"] == "дисплей"
    assert enriched[0]["quality_raw"] == "ORIG"
    assert enriched[0]["model_compatibility"] == "Apple iPad 3 / Apple iPad 4"
    assert enriched[0]["brand_compatibility"] == "Apple"
    assert enriched[0]["characteristic_values"] == {
        "display_type": "In-Cell",
        "display_has_frame": False,
    }
    assert facts[0]["future_ka_mapping_status"] == "ready"
    assert facts[0]["missing_required_attributes"] == []
    assert facts[0]["calculation_unit_level"] == "property_group"


def test_validate_document_line_mapping_reports_missing_receipt_mapping_parts() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE receipt_doc (_IDRRef TEXT, _Date_Time TEXT)"))

    issues = validate_document_line_mapping(
        engine,
        DocumentLineMapping(
            document_table="receipt_doc",
            line_table="receipt_lines",
            line_document_column="_DocumentRRef",
            line_nomenclature_column="_FldNom",
        ),
    )

    assert "table_missing:receipt_lines" in issues
    assert "column_missing:receipt_doc._Marked" in issues
    assert "column_missing:receipt_doc._Posted" in issues


def test_chunks_preserve_all_refs_for_sqlserver_parameter_limit() -> None:
    refs = [f"0x{idx:032X}" for idx in range(5)]

    chunks = list(_chunks(refs, 2))

    assert chunks == [refs[0:2], refs[2:4], refs[4:5]]


def _sales_history_rows() -> list[dict[str, object]]:
    return [
        {
            "nomenclature_ref": "0xA",
            "nomenclature_code": "РБ0001",
            "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
        }
    ]


def test_demand_method_accepts_sales_history_without_third_receipt() -> None:
    # Решение 2026-08-19: два завоза, но товар отработал на полке и продаётся -
    # средняя по доступным дням допустима без третьего поступления.
    facts, _ = build_assortment_lifecycle_fact_records(
        nomenclature_rows=_sales_history_rows(),
        supplier_order_rows=[],
        receipt_rows=[
            {"nomenclature_code": "РБ0001", "receipt_date": date(2026, 5, 4), "qty": 100},
            {"nomenclature_code": "РБ0001", "receipt_date": date(2026, 6, 28), "qty": 120},
        ],
        warehouse_policy=_warehouse_policy(),
        sales_window_totals={
            "РБ0001": {30: Decimal("40"), 90: Decimal("136"), 180: Decimal("168")}
        },
        days_in_sale_totals={"РБ0001": {30: Decimal("21"), 90: Decimal("45"), 180: Decimal("57")}},
        as_of=date(2026, 8, 19),
    )

    assert facts[0]["demand_method_code"] == "available_days_average"
    assert facts[0]["demand_method_reason"] == (
        "Истории продаж достаточно: 57 дней на полке и 136 шт за 90 дней."
    )


def test_demand_method_keeps_manual_review_when_shelf_history_is_short() -> None:
    facts, _ = build_assortment_lifecycle_fact_records(
        nomenclature_rows=_sales_history_rows(),
        supplier_order_rows=[],
        receipt_rows=[
            {"nomenclature_code": "РБ0001", "receipt_date": date(2026, 8, 1), "qty": 100},
        ],
        warehouse_policy=_warehouse_policy(),
        sales_window_totals={"РБ0001": {30: Decimal("40"), 90: Decimal("40"), 180: Decimal("40")}},
        days_in_sale_totals={"РБ0001": {30: Decimal("18"), 90: Decimal("18"), 180: Decimal("18")}},
        as_of=date(2026, 8, 19),
    )

    assert facts[0]["demand_method_code"] == "manual_review"
    assert facts[0]["demand_method_reason"] == "Истории пока мало для безопасного автозаказа."


def test_demand_method_keeps_manual_review_when_sales_are_thin() -> None:
    facts, _ = build_assortment_lifecycle_fact_records(
        nomenclature_rows=_sales_history_rows(),
        supplier_order_rows=[],
        receipt_rows=[
            {"nomenclature_code": "РБ0001", "receipt_date": date(2026, 3, 1), "qty": 20},
        ],
        warehouse_policy=_warehouse_policy(),
        sales_window_totals={"РБ0001": {30: Decimal("1"), 90: Decimal("6"), 180: Decimal("9")}},
        days_in_sale_totals={"РБ0001": {30: Decimal("30"), 90: Decimal("88"), 180: Decimal("150")}},
        as_of=date(2026, 8, 19),
    )

    assert facts[0]["demand_method_code"] == "manual_review"
    assert facts[0]["demand_method_reason"] == "Истории пока мало для безопасного автозаказа."


def test_demand_method_still_prefers_three_receipts() -> None:
    facts, _ = build_assortment_lifecycle_fact_records(
        nomenclature_rows=_sales_history_rows(),
        supplier_order_rows=[],
        receipt_rows=[
            {"nomenclature_code": "РБ0001", "receipt_date": date(2026, 5, 4), "qty": 10},
            {"nomenclature_code": "РБ0001", "receipt_date": date(2026, 6, 28), "qty": 10},
            {"nomenclature_code": "РБ0001", "receipt_date": date(2026, 7, 30), "qty": 10},
        ],
        warehouse_policy=_warehouse_policy(),
        sales_window_totals={"РБ0001": {30: Decimal("0"), 90: Decimal("1"), 180: Decimal("2")}},
        days_in_sale_totals={"РБ0001": {30: Decimal("2"), 90: Decimal("4"), 180: Decimal("6")}},
        as_of=date(2026, 8, 19),
    )

    assert facts[0]["demand_method_code"] == "available_days_average"
    assert facts[0]["demand_method_reason"] == (
        "Есть повторные поступления, можно считать среднюю по доступным дням."
    )
