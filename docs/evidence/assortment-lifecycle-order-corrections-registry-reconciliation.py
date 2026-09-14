"""Сверка «заказано после корректировок» с остатком регистра заказов.

Контрольный случай решения 2026-09-12: заказ РБГУ0000474 сдан в cargo 28.08, но
позиция РБ000076661 снята корректировкой РБГУ0000604. Только чтение 1С.

Запуск из корня рабочей копии:
    PYTHONPATH=. /opt/MM/pricing-service/.venv/bin/python \
        docs/evidence/assortment-lifecycle-order-corrections-registry-reconciliation.py
"""

from __future__ import annotations

import json
import os
from decimal import Decimal

from dotenv import load_dotenv
from sqlalchemy import create_engine

from scripts import sync_open_cargo_supplier_orders_to_bitrix as sync

ORDER_REF = "0xb55e002590803daf11f18745bbd33f21"
CANCELLED_CODE = "РБ000076661"


def total(rows: list[dict], key: str) -> Decimal:
    return sum((Decimal(str(row.get(key) or 0)) for row in rows), Decimal("0"))


def main() -> int:
    load_dotenv("/opt/MM/pricing-service/.env")
    engine = create_engine(os.environ["ONEC_DATABASE_URL"])
    raw = sync.fetch_supplier_order_lines_by_refs(engine, [ORDER_REF]).get(ORDER_REF, [])
    corrected = sync.apply_order_corrections_to_lines(raw)
    report = {
        "order": "РБГУ0000474",
        "lines_before": len(raw),
        "lines_after": len(corrected),
        "ordered_before_corrections": str(total(raw, "quantity")),
        "ordered_after_corrections": str(total(corrected, "quantity")),
        "open_quantity_register": str(total(raw, "open_quantity")),
        "cancelled_code_present_after": any(
            str(line.get("onec_item_code") or "").strip() == CANCELLED_CODE for line in corrected
        ),
        "partially_corrected_examples": [
            {
                "code": str(line.get("onec_item_code") or "").strip(),
                "before": str(line["quantity_before_correction"]),
                "shipping": str(line["quantity"]),
            }
            for line in corrected
            if line.get("quantity_before_correction") is not None
        ][:5],
    }
    report["reconciled"] = (
        report["ordered_after_corrections"] == report["open_quantity_register"]
        and not report["cancelled_code_present_after"]
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["reconciled"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
