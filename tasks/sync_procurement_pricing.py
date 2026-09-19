"""Task 4065: explicit file transport, disabled until the 1C rules are verified."""

import argparse
import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db import get_application_engine
from app.services.exporters.ut103_exchange import resolve_ut103_exchange_root
from app.services.procurement_pricing_exchange import (
    export_approved,
    import_results,
    transport_health,
)
from app.services.procurement_pricing_source import current_prices


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exchange-root")
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args()
    root = Path(resolve_ut103_exchange_root(args.exchange_root))
    settings = get_settings()
    with Session(get_application_engine()) as db:
        result = import_results(db, root)
        if args.export:
            result.update(
                export_approved(
                    db,
                    root,
                    apply_enabled=settings.procurement_pricing_apply_enabled,
                    read_prices=current_prices,
                )
            )
        result["health"] = transport_health(db)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
