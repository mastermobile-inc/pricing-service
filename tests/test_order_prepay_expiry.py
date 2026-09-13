from datetime import UTC, datetime, timedelta
from decimal import Decimal
from xml.etree import ElementTree

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models.order_closure import OrderClosureBatch, OrderClosureEvent, OrderClosureItem
from app.schemas.order_prepay_expiry import SitePrepaySnapshot
from app.services import order_closure as queue
from app.services import order_prepay_expiry as policy

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)


def snapshot(**changes):
    data = dict(
        site_order_id="245000",
        created_at=NOW - timedelta(hours=73),
        observed_at=NOW,
        amount=Decimal("1200.50"),
        currency="RUB",
        payment_system_id=21,
        payment_row_system_id=21,
        payment_row_count=1,
        payment_row_amount=Decimal("1200.50"),
        paid_amount=Decimal(0),
        has_payment_history=False,
        has_shipment_history=False,
        marked=False,
        delivery_allowed=False,
        canceled=False,
        status="N",
    )
    data.update(changes)
    return SitePrepaySnapshot(**data)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            OrderClosureBatch.__table__,
            OrderClosureItem.__table__,
            OrderClosureEvent.__table__,
        ],
    )
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.mark.parametrize("age,allowed", [(259199, False), (259200, False), (259201, True)])
def test_exact_elapsed_seconds(age, allowed):
    assert (
        policy.site_blocker(snapshot(created_at=NOW - timedelta(seconds=age)), NOW) is None
    ) == allowed


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"paid_amount": Decimal("0.01")}, "has_payment"),
        ({"has_payment_history": True}, "has_payment"),
        ({"has_shipment_history": True}, "has_shipment"),
        ({"payment_system_id": 14}, "not_confirmed_prepayment"),
        ({"payment_row_system_id": 22}, "payment_identity_mismatch"),
        ({"payment_row_count": 0}, "payment_identity_mismatch"),
        ({"payment_row_amount": Decimal("1200.49")}, "payment_identity_mismatch"),
        ({"observed_at": NOW - timedelta(seconds=121)}, "site_snapshot_stale"),
        ({"observed_at": NOW + timedelta(seconds=1)}, "site_snapshot_stale"),
        ({"marked": True}, "site_state_conflict"),
        ({"canceled": True}, "site_state_changed"),
    ],
)
def test_site_guards(changes, code):
    assert policy.site_blocker(snapshot(**changes), NOW) == code


def test_naive_clock_and_missing_facts_are_not_defaulted():
    with pytest.raises(ValueError):
        policy.site_blocker(snapshot(), NOW.replace(tzinfo=None))
    data = snapshot().model_dump()
    del data["has_payment_history"]
    with pytest.raises(ValidationError):
        SitePrepaySnapshot(**data)


def test_repeated_discovery_has_one_stable_batch(db):
    first = policy.observe(db, snapshot(), apply_enabled=False, now=NOW)
    second = policy.observe(db, snapshot(), apply_enabled=False, now=NOW)
    assert first.batch_id == second.batch_id
    assert len(db.scalars(select(OrderClosureBatch)).all()) == 1
    assert len(policy.pending_work(db)) == 1
    xml = ElementTree.fromstring(
        queue.render_commands_xml(db.scalars(select(OrderClosureBatch)).all())
    )
    assert xml.find("command").attrib["source_type"] == policy.SOURCE
    assert xml.findtext("command/order/site_created_msk") == "20260904140000"


def test_auto_and_manual_apply_gates_are_independent(db):
    policy.observe(db, snapshot(), apply_enabled=False, now=NOW)
    b = db.scalar(select(OrderClosureBatch))
    b.status = "approved"
    b.command_kind = "apply"
    assert queue.lease_commands(db, allow_apply=True, allow_auto_apply=False, now=NOW) == []
    assert queue.lease_commands(db, allow_apply=False, allow_auto_apply=True, now=NOW) == [b]


def test_observation_cannot_mark_unapplied_order_complete(db):
    policy.observe(db, snapshot(), apply_enabled=False, now=NOW)
    with pytest.raises(queue.OrderClosureConflict):
        policy.observe(db, snapshot(canceled=True, status="D"), apply_enabled=True, now=NOW)
    b = db.scalar(select(OrderClosureBatch))
    assert not b.source_payload["site_canceled"]


def test_full_automatic_lifecycle_requires_fresh_site_and_native_receipt(db):
    from app.schemas.order_closure import OrderClosureCommandAckRequest

    policy.observe(db, snapshot(), apply_enabled=False, now=NOW)
    b = db.scalar(select(OrderClosureBatch))
    queue.lease_commands(db, allow_apply=False, now=NOW)
    item = dict(
        position=1,
        input_number="245000",
        input_period="2026",
        onec_order_ref="11111111-1111-1111-1111-111111111111",
        onec_order_number="РБГУ000001",
        onec_order_date=NOW.date(),
        site_order_number="245000",
        eligible=True,
        state_hash="f" * 64,
        facts={
            "allowed_reasons": {
                "cancellation": {
                    "ref": "22222222-2222-2222-2222-222222222222",
                    "name": "Отмена заказа",
                }
            }
        },
    )
    queue.acknowledge_command(
        db,
        batch=b,
        payload=OrderClosureCommandAckRequest(
            lease_token=b.lease_token,
            outcome="diagnosed",
            diagnosis_hash="d" * 64,
            receipt_hash="e" * 64,
            items=[item],
        ),
        now=NOW,
    )
    policy.observe(db, snapshot(), apply_enabled=False, now=NOW)
    assert b.status == "diagnosed"
    with pytest.raises(queue.OrderClosureConflict, match="has_payment"):
        policy.observe(db, snapshot(has_payment_history=True), apply_enabled=True, now=NOW)
    assert b.status == "diagnosed"
    policy.observe(db, snapshot(), apply_enabled=True, now=NOW)
    assert b.status == "approved" and b.actor_id == policy.ACTOR
    assert policy.pending_work(db)[0].action == "refresh"
    queue.lease_commands(db, allow_apply=False, allow_auto_apply=True, now=NOW)
    result = {
        **item,
        "result_document_ref": "33333333-3333-3333-3333-333333333333",
        "result_document_number": "РБ000000001",
    }
    ack = OrderClosureCommandAckRequest(
        lease_token=b.lease_token,
        outcome="applied",
        diagnosis_hash="d" * 64,
        receipt_hash="c" * 64,
        items=[result],
    )
    queue.acknowledge_command(db, batch=b, payload=ack, now=NOW)
    assert policy.pending_work(db)[0].action == "cancel_site"
    assert queue.acknowledge_command(db, batch=b, payload=ack, now=NOW)[1] is True
    policy.observe(db, snapshot(canceled=True, status="D"), apply_enabled=True, now=NOW)
    assert policy.pending_work(db) == []
    assert len(db.scalars(select(OrderClosureBatch)).all()) == 1


def test_snapshot_identity_changes_never_retarget_existing_batch(db):
    policy.observe(db, snapshot(), apply_enabled=False, now=NOW)
    with pytest.raises(queue.OrderClosureConflict, match="identity"):
        policy.observe(
            db,
            snapshot(amount=Decimal("1"), payment_row_amount=Decimal("1")),
            apply_enabled=True,
            now=NOW,
        )


def test_stale_or_failed_native_command_is_not_blindly_retried(db):
    policy.observe(db, snapshot(), apply_enabled=False, now=NOW)
    b = db.scalar(select(OrderClosureBatch))
    b.status = "stale"
    b.command_kind = None
    result = policy.observe(db, snapshot(), apply_enabled=True, now=NOW)
    assert result.action == "manual_review" and b.command_kind is None


@pytest.mark.parametrize(
    "requested,enabled,apply_enabled",
    [(a, b, c) for a in (False, True) for b in (False, True) for c in (False, True)],
)
def test_command_endpoint_requires_all_automatic_gates(
    db, monkeypatch, requested, enabled, apply_enabled
):
    from types import SimpleNamespace

    from app.api import order_closure as api

    policy.observe(db, snapshot(), apply_enabled=False, now=NOW)
    batch = db.scalar(select(OrderClosureBatch))
    batch.status = "approved"
    batch.command_kind = "apply"
    db.flush()
    monkeypatch.setattr(
        api,
        "get_settings",
        lambda: SimpleNamespace(
            order_closure_apply_enabled=False,
            order_prepay72_enabled=enabled,
            order_prepay72_apply_enabled=apply_enabled,
        ),
    )
    response = api.commands(
        limit=1, allow_apply=False, allow_auto_apply=requested, _token="test", db=db
    )
    commands = ElementTree.fromstring(response.body).findall("command")
    assert len(commands) == int(requested and enabled and apply_enabled)
