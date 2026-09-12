"""One complete read-only Bitrix snapshot; default is a database dry-run."""

import argparse
import json
import time
from datetime import datetime, timezone

import httpx
from dotenv import dotenv_values
from sqlalchemy.orm import Session

from app.api.dependencies import get_engine
from app.core.config import get_settings
from app.services.logistics_drivers import BitrixDriverReader, fetch_snapshot, reconcile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    if args.apply and not settings.logistics_driver_bitrix_sync_enabled:
        raise SystemExit("LOGISTICS_DRIVER_BITRIX_SYNC_ENABLED is disabled")
    base = settings.logistics_driver_bitrix_webhook_url
    credential_file = getattr(settings, "logistics_driver_bitrix_webhook_env_file", None)
    if not base and credential_file:
        # Explicitly configured service connection; never probe other webhooks or
        # import unrelated DB/runtime settings from an orchestration environment.
        base = dotenv_values(credential_file, interpolate=False).get(
            "AI_TASK_ANALYSIS_BITRIX_WEBHOOK_BASE"
        )
    if not base:
        raise SystemExit("LOGISTICS_DRIVER_BITRIX_WEBHOOK_URL is not configured")
    started = time.monotonic()
    try:
        with httpx.Client(timeout=6, follow_redirects=False) as client:
            rows, read = fetch_snapshot(BitrixDriverReader(base, client))
        with Session(get_engine()) as session:
            report = reconcile(session, rows, apply=args.apply)
        print(
            json.dumps(
                dict(
                    **report,
                    success=True,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                    read=read,
                    apply=args.apply,
                    duration_seconds=round(time.monotonic() - started, 2),
                    drivers=[
                        {
                            "bitrix_user_id": row["bitrix_user_id"],
                            "full_name": row["full_name"],
                            "shift_status": row["shift_status"],
                        }
                        for row in rows
                    ],
                ),
                ensure_ascii=False,
            )
        )
    except Exception as exc:
        # Do not print DB/webhook exception values or connection strings.
        print(
            json.dumps(
                {
                    "success": False,
                    "error_type": type(exc).__name__,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "duration_seconds": round(time.monotonic() - started, 2),
                }
            )
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
