"""Restricted test entrypoint; never mount event, payment or shipment writers."""

import os
import time
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Query, Response
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, require_order_fulfillment_internal_token
from app.api.order_fulfillment import assembly_queue_response
from app.core.config import get_settings

STATE_ROOT = Path("/opt/MM/.local/task46-test-assembly")


class TestQueueSnapshot:
    """Coalesce UI reads only; never extend timestamps or return expired/error data."""

    __test__ = False

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.lock = Lock()
        self.cached: tuple[int, float, bytes] | None = None

    def get(self, limit: int, refresh: Callable[[], Response]) -> Response:
        with self.lock:
            now = self.clock()
            if self.cached is not None:
                key, until, body = self.cached
                if key == limit and now < until:
                    return Response(body, media_type="application/xml")
            self.cached = None
            response = refresh()
            # Age starts BEFORE the upstream read, not at response completion.
            if response.status_code == 200 and self.clock() < now + 15:
                self.cached = (limit, now + 15, bytes(response.body))
            return response


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
    snapshot = TestQueueSnapshot()

    @app.get(
        "/api/order-fulfillment/assembly-queue",
        dependencies=[Depends(require_order_fulfillment_internal_token)],
    )
    def queue(
        format: str = Query(default="xml", pattern="^xml$"),
        limit: int = Query(default=2000, ge=1, le=2000),
        db: Session = Depends(get_db),
    ) -> Response:
        del format
        return snapshot.get(
            limit, lambda: assembly_queue_response(db, limit=limit, maximum_limit=2000)
        )

    return app
