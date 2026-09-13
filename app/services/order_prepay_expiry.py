from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from app.schemas.order_prepay_expiry import SitePrepaySnapshot

SOURCE = "auto_prepay72"
ACTOR = "automation:prepay72:v1"
MIN_AGE = timedelta(hours=72)
MAX_SNAPSHOT_AGE = timedelta(seconds=120)
# Explicit online-prepayment systems confirmed on the site. No cash/postpayment.
PREPAY_SYSTEMS = frozenset({10, 12, 16, 17, 20, 21, 22})


def site_blocker(
    snapshot: SitePrepaySnapshot, now: datetime, *, canceled: bool = False
) -> str | None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone-aware current time is required")
    now = now.astimezone(UTC)
    if not timedelta(0) <= now - snapshot.observed_at <= MAX_SNAPSHOT_AGE:
        return "site_snapshot_stale"
    if snapshot.created_at > snapshot.observed_at:
        return "site_creation_invalid"
    if now - snapshot.created_at <= MIN_AGE:
        return "prepayment_not_expired"
    if snapshot.payment_system_id not in PREPAY_SYSTEMS:
        return "not_confirmed_prepayment"
    if (
        snapshot.payment_row_count != 1
        or snapshot.payment_row_system_id != snapshot.payment_system_id
        or snapshot.payment_row_amount != snapshot.amount
    ):
        return "payment_identity_mismatch"
    if snapshot.has_payment_history or snapshot.paid_amount != 0:
        return "has_payment"
    if snapshot.has_shipment_history:
        return "has_shipment"
    if snapshot.marked or snapshot.delivery_allowed:
        return "site_state_conflict"
    if snapshot.canceled != canceled or snapshot.status != ("D" if canceled else "N"):
        return "site_state_changed"
    return None


def batch_id(site_order_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"mm:ut103:prepay72:v1:{site_order_id}"))


def command_evidence(snapshot: SitePrepaySnapshot) -> dict[str, str]:
    """Native 8.2 uses explicit Moscow wall time; no platform ISO parser needed."""
    from zoneinfo import ZoneInfo

    moscow = ZoneInfo("Europe/Moscow")
    return {
        "prepay_policy": "web_prepay_72h_v1",
        "site_order_id": snapshot.site_order_id,
        "site_created_msk": snapshot.created_at.astimezone(moscow).strftime("%Y%m%d%H%M%S"),
        "site_observed_msk": snapshot.observed_at.astimezone(moscow).strftime("%Y%m%d%H%M%S"),
        "site_amount": format(snapshot.amount, ".2f"),
        "site_amount_minor": str(int(snapshot.amount * 100)),
        "site_prepayment_system": str(snapshot.payment_system_id),
    }


def _work(batch):
    from app.schemas.order_prepay_expiry import PrepayWorkItem

    item = batch.items[0]
    if batch.source_payload.get("site_canceled"):
        action = "complete"
    elif batch.status in {"failed", "stale"}:
        action = "manual_review"
    elif batch.status == "applied":
        action = "cancel_site"
    else:
        action = "refresh"
    return PrepayWorkItem(
        batch_id=batch.public_id,
        site_order_id=batch.source_payload["site_order_id"],
        action=action,
        closure_document_ref=item.result_document_ref,
        closure_document_number=item.result_document_number,
        reason=batch.last_error_code,
        expected_created_at=batch.source_payload["site_snapshot"]["created_at"],
        expected_amount=batch.source_payload["site_snapshot"]["amount"],
        payment_system_id=batch.source_payload["site_snapshot"]["payment_system_id"],
    )


def pending_work(session, *, limit: int = 20):
    from sqlalchemy import select

    from app.models.order_closure import OrderClosureBatch

    batches = session.scalars(
        select(OrderClosureBatch)
        .where(
            OrderClosureBatch.source_type == SOURCE,
            OrderClosureBatch.actor_id == ACTOR,
            OrderClosureBatch.status.in_(("draft", "diagnosed", "approved", "leased", "applied")),
            OrderClosureBatch.source_payload["site_canceled"].as_boolean().is_not(True),
        )
        .order_by(OrderClosureBatch.created_at, OrderClosureBatch.id)
        .limit(min(max(limit, 1), 20))
    ).all()
    return [_work(batch) for batch in batches]


def observe(session, snapshot: SitePrepaySnapshot, *, apply_enabled: bool, now: datetime):
    """One stable batch per WEB identity; no automatic retries of a stale/failed apply."""
    from sqlalchemy import select

    from app.models.order_closure import OrderClosureBatch, OrderClosureItem
    from app.schemas.order_closure import OrderClosureConfirmRequest, OrderClosureReasonAssignment
    from app.services import order_closure as queue

    public_id = batch_id(snapshot.site_order_id)
    batch = session.scalar(
        select(OrderClosureBatch).where(OrderClosureBatch.public_id == public_id).with_for_update()
    )
    if batch is not None:
        if batch.source_type != SOURCE or batch.actor_id != ACTOR or len(batch.items) != 1:
            raise queue.OrderClosureConflict("automatic batch identity mismatch")
        original = SitePrepaySnapshot.model_validate_json(
            json.dumps(batch.source_payload["site_snapshot"])
        )
        if any(
            getattr(snapshot, name) != getattr(original, name)
            for name in ("site_order_id", "created_at", "amount", "currency", "payment_system_id")
        ):
            raise queue.OrderClosureConflict("site order identity or amount changed")
        if batch.status == "applied":
            # The site collector retries native cancellation only after this receipt.
            if site_blocker(snapshot, now, canceled=True) is None:
                batch.source_payload = {**batch.source_payload, "site_canceled": True}
                queue._event(session, batch, "site_cancellation_verified", ACTOR)
                session.flush()
            return _work(batch)
        if batch.status in {"failed", "stale", "leased"}:
            return _work(batch)
    blocker = site_blocker(snapshot, now)
    if blocker:
        raise queue.OrderClosureConflict(blocker)
    if batch is None:
        batch = OrderClosureBatch(
            public_id=public_id,
            source_type=SOURCE,
            source_payload={
                "site_order_id": snapshot.site_order_id,
                "site_snapshot": snapshot.model_dump(mode="json"),
                "site_canceled": False,
                "policy": "web_prepay_72h_v1",
            },
            actor_id=ACTOR,
            actor_name="Автоматическое закрытие предоплаты через 72 часа",
            status="draft",
            command_kind="diagnose",
            command_requested_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(batch)
        session.flush()
        session.add(
            OrderClosureItem(
                batch=batch,
                position=1,
                input_number=snapshot.site_order_id,
                input_period=str(snapshot.created_at.year),
                status="pending",
                eligible=False,
                facts={},
                created_at=now,
                updated_at=now,
            )
        )
        queue._event(session, batch, "automatic_prepay_candidate", ACTOR)
        session.flush()
    else:
        batch.source_payload = {
            **batch.source_payload,
            "site_snapshot": snapshot.model_dump(mode="json"),
        }
    if batch.status == "diagnosed":
        item = batch.items[0]
        if (
            not item.eligible
            or item.site_order_number != snapshot.site_order_id
            or not item.onec_order_ref
            or not item.state_hash
        ):
            batch.status = "failed"
            batch.last_error_code = item.blocker_code or "automatic_diagnosis_not_eligible"
            queue._event(session, batch, "automatic_manual_review", ACTOR)
        elif apply_enabled:
            reason = (item.facts or {}).get("allowed_reasons", {}).get("cancellation", {})
            if not reason.get("ref"):
                raise queue.OrderClosureConflict("cancellation reason not diagnosed")
            queue.confirm_batch(
                session,
                batch=batch,
                payload=OrderClosureConfirmRequest(
                    diagnosis_hash=batch.diagnosis_hash,
                    assignments=[
                        OrderClosureReasonAssignment(
                            item_id=item.id,
                            reason_code="cancellation",
                            reason_name="Отмена заказа",
                            reason_ref=reason["ref"],
                        )
                    ],
                ),
                actor=queue.Actor(ACTOR, batch.actor_name, True),
                now=now,
            )
    session.flush()
    return _work(batch)
