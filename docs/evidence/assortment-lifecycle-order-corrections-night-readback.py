"""Однократная read-only проверка ночного результата установки корректировок.

Запускается после следующего штатного окна. Не пересчитывает и не меняет данные;
записывает только локальный отчёт. Нулевая очередь переходов допустима.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.infrastructure.db.engines import build_readonly_postgres_engine  # noqa: E402
from app.services.assortment_lifecycle import decide_assortment_status  # noqa: E402
from tasks.build_assortment_lifecycle_updates import _lifecycle_input_from_record  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-date", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--baseline-run-id", required=True, type=int)
    parser.add_argument("--control-code", required=True)
    parser.add_argument("--cancelled-cargo-date", required=True)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    report = {"observed_at": datetime.now(ZoneInfo("Europe/Moscow")).isoformat()}
    try:
        active = Path("/opt/MM/pricing-service-task43-current").resolve()
        manifest = json.loads((active / "release-manifest.json").read_text())
        night = json.loads(Path("/var/lib/pricing/assortment-nightly.json").read_text())
        load_dotenv(ROOT / ".env")
        engine = build_readonly_postgres_engine(get_settings().database_url)
        try:
            with engine.connect() as conn:
                run = dict(
                    conn.execute(
                        text(
                            "SELECT * FROM assortment_lifecycle_classification_run "
                            "WHERE folder=:folder ORDER BY id DESC LIMIT 1"
                        ),
                        {"folder": "дисплеи"},
                    )
                    .mappings()
                    .one()
                )
                control = dict(
                    conn.execute(
                        text(
                            "SELECT nomenclature_code,status,source_record,last_run_id "
                            "FROM assortment_lifecycle_classification WHERE nomenclature_code=:code"
                        ),
                        {"code": args.control_code},
                    )
                    .mappings()
                    .one()
                )
                rows = conn.execute(
                    text(
                        "SELECT count(*) FROM assortment_lifecycle_classification "
                        "WHERE last_run_id=:run_id"
                    ),
                    {"run_id": run["id"]},
                ).scalar_one()
                transitions = [
                    dict(row)
                    for row in conn.execute(
                        text(
                            "SELECT status,count(*) AS count FROM procurement_lifecycle_transition_proposal "
                            "WHERE run_id=:run_id GROUP BY status"
                        ),
                        {"run_id": run["id"]},
                    ).mappings()
                ]
        finally:
            engine.dispose()
        facts = control.pop("source_record")
        calculated = decide_assortment_status(_lifecycle_input_from_record(facts)).status.value
        # Отменённая отправка не должна оставаться фактом контрольной карточки.
        cargo_dates = facts.get("supplier_order_cargo_handoff_dates") or []
        checks = {
            "expected_release": manifest.get("source_commit") == args.expected_commit,
            "night_success": night.get("status") == "success"
            and night.get("date") == args.expected_date,
            "new_classification_run": run["id"] > args.baseline_run_id
            and run["source_status"] == "ready",
            "night_run_matches": str(run["started_at"])[:10] == args.expected_date,
            "snapshot_complete": rows == run["items_total"] and rows > 0,
            "control_refreshed": control["last_run_id"] == run["id"],
            "control_matches_formula": control["status"] == calculated,
            "cancelled_cargo_removed": args.cancelled_cargo_date not in cargo_dates
            and facts.get("historical_first_cargo_handoff_at") != args.cancelled_cargo_date,
        }
        report.update(
            status="passed" if all(checks.values()) else "needs_attention",
            checks=checks,
            night=night,
            run={
                key: run[key]
                for key in ("id", "source_status", "items_total", "started_at", "finished_at")
            },
            control=control,
            transitions=transitions,
        )
    except Exception as exc:
        report.update(status="needs_attention", error_type=type(exc).__name__)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    print(json.dumps({"status": report["status"], "report": str(args.report)}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
