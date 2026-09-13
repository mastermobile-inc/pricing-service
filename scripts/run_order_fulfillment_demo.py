#!/usr/bin/env python3
"""Изолированная тестовая корзина, только loopback; никаких внешних интеграций."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from starlette.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402

from app.schemas.order_fulfillment_quote import FulfillmentQuoteRequest  # noqa: E402
from app.services.order_fulfillment_quote import (  # noqa: E402
    InventorySnapshot,
    QuoteUnavailable,
    TimingProfile,
    calculate_quote,
)

DEFAULT_PROFILE = (
    ROOT.parent
    / "1C_Dev_Workflow/migration/ut103/tests/task-46-fulfillment-timing-test-profile.json"
)
DEFAULT_SITE = ROOT.parent / "mastermobile-site-task3520-review"


def create_demo_app(
    profile_path: Path = DEFAULT_PROFILE, site_root: Path = DEFAULT_SITE
) -> FastAPI:
    # Deliberately NOT app.main: no jobs, DB, payment, shipping or 1C bootstrap.
    app = FastAPI(title="Тестовая корзина №46", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )

    @app.middleware("http")
    async def isolation(request: Request, call_next):
        if request.client and request.client.host not in {"127.0.0.1", "::1", "testclient"}:
            return JSONResponse({"detail": "loopback_only"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "cross_origin_forbidden"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-MM-Environment"] = "task46-synthetic-demo-no-orders"
        return response

    def inputs(now):
        profile = TimingProfile.model_validate_json(profile_path.read_text(encoding="utf-8"))
        data = json.loads(
            (ROOT / "tests/fixtures/order_fulfillment_inventory.json").read_text(encoding="utf-8")
        )
        # This is synthetic regeneration, NOT a refreshed 1C observation.
        data["observed_at"] = now.isoformat()
        return profile, InventorySnapshot.model_validate(data)

    @app.get("/demo/bootstrap")
    def bootstrap():
        _, stock = inputs(datetime.now(timezone.utc))
        return {
            "environment": "synthetic_fixture",
            "test_only": True,
            "warehouses": [
                w.model_dump() for w in stock.warehouses if w.active and not w.technical
            ],
            "lines": [
                {
                    "line_id": "demo-line-1",
                    "product_id": "demo-display",
                    "name": "Дисплей · демонстрационный товар",
                    "quantity": "2",
                    "unit_id": "piece",
                },
                {
                    "line_id": "demo-line-2",
                    "product_id": "demo-battery",
                    "name": "Аккумулятор · демонстрационный товар",
                    "quantity": "1",
                    "unit_id": "piece",
                },
            ],
        }

    @app.post("/api/order-fulfillment/quote")
    def quote(payload: FulfillmentQuoteRequest):
        now = datetime.now(timezone.utc)
        try:
            profile, inventory = inputs(now)
            return calculate_quote(payload, profile, inventory, now=now)
        except QuoteUnavailable as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail="test_configuration_invalid") from exc

    # Exact allowlist, never mount the site checkout, .env, PHP or directory trees.
    files = {
        "/": ("local/tools/order-fulfillment-demo.html", "text/html"),
        "/mm_order_fulfillment.js": (
            "local/templates/new/js/mm_order_fulfillment.js",
            "text/javascript",
        ),
        "/mm_order_fulfillment.css": (
            "local/templates/new/css/mm_order_fulfillment.css",
            "text/css",
        ),
    }
    for url, (relative, media_type) in files.items():

        # No parameterized file endpoint: FastAPI must not expose defaults as query args.
        def make_handler(file_path, mime_type):
            def handler():
                return FileResponse(file_path, media_type=mime_type)

            return handler

        app.add_api_route(url, make_handler(site_root / relative, media_type), methods=["GET"])
    return app


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18946)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--site-root", type=Path, default=DEFAULT_SITE)
    args = parser.parse_args()
    uvicorn.run(create_demo_app(args.profile, args.site_root), host="127.0.0.1", port=args.port)
