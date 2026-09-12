"""Bounded adapter to the mm-compensation owned, shared resolver.

The release config supplies a fixed argv (Python + immutable worker path). Only
an integer site user id is appended. No shell or browser-provided identities.
"""

import json
import os
import signal
import subprocess
from datetime import datetime, timezone
from threading import BoundedSemaphore
from uuid import uuid4

from app.core.config import get_settings
from app.schemas.site_customer_prices import SiteCustomerPriceResponse

_SLOTS = BoundedSemaphore(2)


def resolve_site_customer_price(user_id: int) -> SiteCustomerPriceResponse:
    fallback = dict(
        user_id=user_id,
        status="unavailable",
        reason="source_check_failed",
        checked_at=datetime.now(timezone.utc),
        check_id=uuid4().hex,
    )
    settings = get_settings()
    if not settings.site_customer_prices_enabled or not _SLOTS.acquire(blocking=False):
        return SiteCustomerPriceResponse(**fallback)
    try:
        command = json.loads(settings.site_customer_prices_resolver_argv or "[]")
        if (
            not isinstance(command, list)
            or len(command) < 2
            or not all(isinstance(x, str) and x for x in command)
            or not os.path.isabs(command[0])
        ):
            return SiteCustomerPriceResponse(**fallback)
        with subprocess.Popen(
            [*command, "--user-id", str(user_id)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        ) as process:
            try:
                output, _ = process.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                return SiteCustomerPriceResponse(**fallback)
            if process.returncode:
                return SiteCustomerPriceResponse(**fallback)
        raw = json.loads(output)
        if int(raw.get("user_id", 0)) != user_id:
            return SiteCustomerPriceResponse(**fallback)
        if raw.get("status", "").startswith("pending"):
            raw.update(status="conflict", reason="link_sync_pending")
        if raw.get("status") == "excluded":
            raw.update(status="conflict")
        result = SiteCustomerPriceResponse.model_validate(raw)
        age = (datetime.now(timezone.utc) - result.checked_at).total_seconds()
        if not 0 <= age <= 60:
            return SiteCustomerPriceResponse(**fallback)
        if result.status in ("confirmed", "retail") and (
            not result.price_type or not result.catalog_group_id
        ):
            return SiteCustomerPriceResponse(**fallback)
        return result
    except (OSError, ValueError, TypeError):
        return SiteCustomerPriceResponse(**fallback)
    finally:
        _SLOTS.release()
