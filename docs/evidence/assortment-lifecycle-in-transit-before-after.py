"""Прогон «было/стало» для решения 2026-09-11 («В пути» / «Завезли»).

Сравнивает формулу активного релиза с формулой текущего дерева на одних и тех же
сохранённых фактах классификации. Только чтение: во внешние системы и в базу
ничего не пишется.

Запуск из корня рабочей копии:
    PYTHONPATH=. /opt/MM/pricing-service/.venv/bin/python \
        docs/evidence/assortment-lifecycle-in-transit-before-after.py
"""

from __future__ import annotations

import collections
import importlib.util
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ACTIVE_RELEASE = Path("/opt/MM/pricing-service-task43-current")
ENV_FILE = Path("/opt/MM/pricing-service/.env")
FRESH_RUN_DAY = "2026-09-11"


def load_release_formula():
    """Формула «до» — из активного неизменяемого релиза."""
    module_path = ACTIVE_RELEASE / "app/services/assortment_lifecycle.py"
    spec = importlib.util.spec_from_file_location("release_assortment_lifecycle", module_path)
    module = importlib.util.module_from_spec(spec)
    # dataclass внутри модуля ищет себя в sys.modules — без регистрации падает.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.decide_assortment_status


def main() -> int:
    load_dotenv(ENV_FILE)
    sys.path.insert(0, str(Path.cwd()))
    from app.services.assortment_lifecycle import ASSORTMENT_STATUS_LABELS
    from app.services.assortment_lifecycle import decide_assortment_status as new_decide
    from tasks.build_assortment_lifecycle_updates import _lifecycle_input_from_record

    old_decide = load_release_formula()
    engine = create_engine(os.environ["DATABASE_URL"])
    buckets: dict[str, collections.Counter] = {
        "fresh_display_run": collections.Counter(),
        "frozen_full_catalog_snapshot": collections.Counter(),
    }
    totals: collections.Counter = collections.Counter()
    samples: dict[str, list[str]] = collections.defaultdict(list)

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT nomenclature_code, name, classified_at, source_record "
                "FROM assortment_lifecycle_classification"
            )
        ).mappings()
        for row in rows:
            record = row["source_record"]
            if not isinstance(record, dict):
                continue
            bucket = (
                "fresh_display_run"
                if row["classified_at"].date().isoformat() == FRESH_RUN_DAY
                else "frozen_full_catalog_snapshot"
            )
            totals[bucket] += 1
            lifecycle_input = _lifecycle_input_from_record(record)
            was = str(old_decide(lifecycle_input).status)
            now = str(new_decide(lifecycle_input).status)
            buckets[bucket][(was, now)] += 1
            key = f"{bucket}:{was}->{now}"
            if was != now and len(samples[key]) < 3:
                samples[key].append(f"{row['nomenclature_code']} · {(row['name'] or '')[:48]}")

    report: dict[str, object] = {"database_write": False, "external_write": False, "buckets": {}}
    for bucket, counter in buckets.items():
        transitions = []
        for (was, now), count in sorted(counter.items(), key=lambda item: -item[1]):
            if was == now:
                continue
            transitions.append(
                {
                    "from": was,
                    "from_label": ASSORTMENT_STATUS_LABELS.get(was, was),
                    "to": now,
                    "to_label": ASSORTMENT_STATUS_LABELS.get(now, now),
                    "count": count,
                    "samples": samples[f"{bucket}:{was}->{now}"],
                }
            )
        report["buckets"][bucket] = {
            "cards": totals[bucket],
            "changed": sum(item["count"] for item in transitions),
            "transitions": transitions,
        }

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
