"""Minute transfer sync; 1C is read-only, application writes require --apply."""

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine, build_onec_engine
from app.services.logistics import normalize_mm_log_document_ref
from app.services.logistics_transfer_sync import sync_all


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--date-from", type=date.fromisoformat)
    parser.add_argument(
        "--limit", type=int, default=500, help="Page size, not total limit (1..5000)"
    )
    parser.add_argument("--transfer-external-id", help="1C document _IDRRef or decimal UUID")
    parser.add_argument("--document-number", help="Exact full 1C document number")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 5000:
        parser.error("--limit must be between 1 and 5000")
    if args.transfer_external_id:
        args.transfer_external_id = normalize_mm_log_document_ref(args.transfer_external_id)
        if not args.transfer_external_id:
            parser.error("Invalid transfer reference")
    return args


def main():
    args = parse_args()
    settings = get_settings()
    if args.apply and not settings.logistics_transfer_sync_enabled:
        raise SystemExit("LOGISTICS_TRANSFER_SYNC_ENABLED is disabled")
    if not settings.onec_database_url:
        raise SystemExit("ONEC_DATABASE_URL is not configured")
    date_from = args.date_from
    if date_from is None and not args.transfer_external_id and not args.document_number:
        date_from = date.today() - timedelta(days=14)
    started = time.monotonic()
    engines = []
    try:
        engines.append(build_engine(settings.database_url, pool_pre_ping=True))
        engines.append(
            build_onec_engine(
                settings.onec_database_url, query_timeout_seconds=40, login_timeout_seconds=5
            )
        )
        with Session(engines[0]) as session:
            report = sync_all(
                session,
                engines[1],
                date_from=date_from,
                page_size=args.limit,
                apply=args.apply,
                external_id=args.transfer_external_id,
                document_number=args.document_number,
                on_page=lambda page: print(
                    json.dumps({**page, "elapsed_seconds": round(time.monotonic() - started, 3)}),
                    file=sys.stderr,
                    flush=True,
                ),
            )
        print(
            json.dumps(
                {
                    **report,
                    "success": True,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "duration_seconds": round(time.monotonic() - started, 3),
                },
                ensure_ascii=False,
            )
        )
    except Exception as exc:
        # DB exception strings may contain credentials or connection parameters.
        print(
            json.dumps(
                {
                    "success": False,
                    "error_type": type(exc).__name__,
                    "duration_seconds": round(time.monotonic() - started, 3),
                }
            )
        )
        raise SystemExit(1) from None
    finally:
        for engine in engines:
            engine.dispose()


if __name__ == "__main__":
    main()
