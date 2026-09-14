"""Restricted test entrypoint; never mount event, payment or shipment writers."""

import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.routing import APIRoute
from sqlalchemy.engine import make_url

from app.api.order_fulfillment import router
from app.core.config import get_settings

STATE_ROOT = Path("/opt/MM/.local/task46-test-assembly")


def validate_runtime(environment: str, database_url: str | None) -> None:
    if environment != "test" or not database_url:
        raise ValueError("assembly_test_environment_required")
    url = make_url(database_url)
    if url.drivername != "sqlite" or url.query or not url.database:
        raise ValueError("assembly_isolated_database_required")
    if Path(url.database).resolve() != STATE_ROOT / "queue.sqlite":
        raise ValueError("assembly_isolated_database_required")


def create_app() -> FastAPI:
    settings = get_settings()
    validate_runtime(settings.environment, settings.database_url)
    source = os.environ.get("MM_TASK46_CRM_SOURCE", "")
    url = urlsplit(settings.order_fulfillment_bitrix_webhook_url or "")
    if source not in {"test", "production_readonly"}:
        raise ValueError("assembly_crm_source_approval_required")
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("assembly_crm_https_source_required")
    if url.query or url.fragment:
        raise ValueError("assembly_crm_source_invalid")
    if source == "test" and url.hostname == "crm.master-mobile.ru":
        raise ValueError("production_crm_is_not_test")
    if source == "production_readonly" and url.hostname != "crm.master-mobile.ru":
        raise ValueError("assembly_crm_source_mismatch")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    selected = [
        route
        for route in router.routes
        if isinstance(route, APIRoute)
        and route.path == "/assembly-queue"
        and route.methods == {"GET"}
    ]
    if len(selected) != 1:
        raise RuntimeError("assembly_route_identity_changed")
    # Reuse the actual endpoint, dependencies and error handling without
    # mounting the other (mutating) order-fulfillment endpoints.
    from fastapi import APIRouter

    restricted = APIRouter()
    restricted.routes.extend(selected)
    app.include_router(restricted, prefix="/api/order-fulfillment")
    return app
