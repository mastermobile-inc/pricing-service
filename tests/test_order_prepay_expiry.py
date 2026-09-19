from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID
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


def clock(*transitions):
    return dict(
        policy="web_prepay_72h_v2",
        source="site_gate_v1",
        valid_until=NOW + timedelta(seconds=120),
        enrollment_id=UUID(int=1),
        enrolled_at=NOW - timedelta(days=10),
        events=[
            dict(event_id=UUID(int=i + 10), sequence=i, occurred_at=at, available=allowed)
            for i, (at, allowed) in enumerate(transitions, 1)
        ],
    )


def snapshot(**changes):
    data = dict(
        site_order_id="245000",
        created_at=NOW - timedelta(days=10),
        payment_clock=clock((NOW - timedelta(hours=73), True)),
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


def prepared_snapshot(**changes):
    data = dict(
        payment_clock=clock((NOW - timedelta(hours=73), True), (NOW, False)),
        closure_hold=dict(
            id=UUID(int=99), batch_id=policy.batch_id("245000"), frozen_at=NOW, clock_revision=2
        ),
    )
    data.update(changes)
    return snapshot(**data)


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
        policy.site_blocker(
            snapshot(payment_clock=clock((NOW - timedelta(seconds=age), True))), NOW
        )
        is None
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
    first = policy.observe(db, prepared_snapshot(), apply_enabled=False, now=NOW)
    second = policy.observe(db, prepared_snapshot(), apply_enabled=False, now=NOW)
    assert first.batch_id == second.batch_id
    assert len(db.scalars(select(OrderClosureBatch)).all()) == 1
    assert len(policy.pending_work(db)) == 1
    xml = ElementTree.fromstring(
        queue.render_commands_xml(db.scalars(select(OrderClosureBatch)).all())
    )
    assert xml.find("command").attrib["source_type"] == policy.SOURCE
    assert xml.findtext("command/order/site_created_msk") == "20260828150000"


def test_auto_and_manual_apply_gates_are_independent(db):
    policy.observe(db, prepared_snapshot(), apply_enabled=False, now=NOW)
    b = db.scalar(select(OrderClosureBatch))
    b.status = "approved"
    b.command_kind = "apply"
    assert queue.lease_commands(db, allow_apply=True, allow_auto_apply=False, now=NOW) == []
    assert queue.lease_commands(db, allow_apply=False, allow_auto_apply=True, now=NOW) == [b]


def test_observation_cannot_mark_unapplied_order_complete(db):
    policy.observe(db, snapshot(), apply_enabled=False, now=NOW)
    result = policy.observe(db, snapshot(canceled=True, status="D"), apply_enabled=True, now=NOW)
    b = db.scalar(select(OrderClosureBatch))
    assert not b.source_payload["site_canceled"]
    assert result.action == "manual_review" and b.command_kind is None


def test_full_automatic_lifecycle_requires_fresh_site_and_native_receipt(db):
    from app.schemas.order_closure import OrderClosureCommandAckRequest

    policy.observe(db, prepared_snapshot(), apply_enabled=False, now=NOW)
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
    policy.observe(db, prepared_snapshot(), apply_enabled=False, now=NOW)
    assert b.status == "diagnosed"
    policy.observe(db, prepared_snapshot(), apply_enabled=True, now=NOW)
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
    policy.observe(db, prepared_snapshot(canceled=True, status="D"), apply_enabled=True, now=NOW)
    assert policy.pending_work(db) == []
    assert len(db.scalars(select(OrderClosureBatch)).all()) == 1


def test_snapshot_identity_changes_never_retarget_existing_batch(db):
    policy.observe(db, snapshot(), apply_enabled=False, now=NOW)
    b = db.scalar(select(OrderClosureBatch))
    b.status = "approved"
    b.command_kind = "apply"
    result = policy.observe(
        db,
        snapshot(amount=Decimal("1"), payment_row_amount=Decimal("1")),
        apply_enabled=True,
        now=NOW,
    )
    assert result.action == "manual_review"
    assert b.last_error_code == "site_order_identity_changed" and b.command_kind is None
    assert b.source_payload["site_snapshot"]["amount"] == "1200.50"
    assert queue.lease_commands(db, allow_auto_apply=True, now=NOW) == []


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

    policy.observe(db, prepared_snapshot(), apply_enabled=False, now=NOW)
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
    monkeypatch.setattr(queue, "_now", lambda value=None: value or NOW)
    response = api.commands(
        limit=1, allow_apply=False, allow_auto_apply=requested, _token="test", db=db
    )
    commands = ElementTree.fromstring(response.body).findall("command")
    assert len(commands) == int(requested and enabled and apply_enabled)


def test_old_order_with_recent_payment_opening_is_not_expired():
    s = snapshot(payment_clock=clock((NOW - timedelta(hours=1), True)))
    assert policy.site_blocker(s, NOW) == "prepayment_not_expired"


def test_pause_preserves_remaining_time_and_resume_does_not_reset():
    s = snapshot(
        payment_clock=clock(
            (NOW - timedelta(hours=100), True),
            (NOW - timedelta(hours=70), False),
            (NOW - timedelta(hours=42), True),
        )
    )
    assert policy.site_blocker(s, NOW) == "prepayment_not_expired"  # 30 + 42 = 72
    assert (
        policy.site_blocker(
            s.model_copy(update={"observed_at": NOW + timedelta(seconds=1)}),
            NOW + timedelta(seconds=1),
        )
        is None
    )
    paused = snapshot(payment_clock=clock((NOW - timedelta(hours=100), True), (NOW, False)))
    assert policy.site_blocker(paused, NOW) == "payment_clock_paused"


def test_legacy_snapshot_cannot_close_even_when_older_than_72_hours(db):
    with pytest.raises(queue.OrderClosureConflict, match="payment_clock_missing"):
        policy.observe(db, snapshot(payment_clock=None), apply_enabled=True, now=NOW)
    assert db.scalar(select(OrderClosureBatch)) is None


def test_old_batches_cannot_be_leased_even_with_all_apply_flags_enabled(db):
    policy.observe(db, snapshot(), apply_enabled=False, now=NOW)
    b = db.scalar(select(OrderClosureBatch))
    b.actor_id = "automation:prepay72:v1"
    b.status = "approved"
    b.command_kind = "apply"
    assert queue.lease_commands(db, allow_apply=True, allow_auto_apply=True, now=NOW) == []


def test_missing_reordered_or_repeated_events_never_credit_time():
    from copy import deepcopy

    c = clock(
        (NOW - timedelta(hours=100), True),
        (NOW - timedelta(hours=50), False),
        (NOW - timedelta(hours=49), True),
    )
    for change in ("gap", "duplicate", "reversed", "future", "same_state"):
        broken = deepcopy(c)
        if change == "gap":
            broken["events"].pop(1)
        elif change == "duplicate":
            broken["events"][1]["event_id"] = broken["events"][0]["event_id"]
        elif change == "reversed":
            broken["events"][1]["occurred_at"] = NOW - timedelta(hours=101)
        elif change == "future":
            broken["events"][-1]["occurred_at"] = NOW + timedelta(seconds=1)
        else:
            broken["events"][1]["available"] = True
        assert policy.site_blocker(snapshot(payment_clock=broken), NOW).startswith("payment_clock_")


def test_history_cannot_be_rewritten_to_expire_an_existing_order(db):
    policy.observe(db, snapshot(), apply_enabled=False, now=NOW)
    changed = snapshot(payment_clock=clock((NOW - timedelta(hours=99), True)))
    result = policy.observe(db, changed, apply_enabled=True, now=NOW)
    b = db.scalar(select(OrderClosureBatch))
    assert result.action == "manual_review"
    assert b.last_error_code == "payment_clock_history_rewritten"
    assert b.command_kind is None


def test_seconds_between_observation_and_processing_do_not_make_order_expired():
    s = snapshot(payment_clock=clock((NOW - timedelta(hours=72), True)))
    assert policy.site_blocker(s, NOW + timedelta(seconds=120)) == "prepayment_not_expired"


def test_native_evidence_contains_payment_clock_not_fake_creation_time():
    evidence = policy.command_evidence(snapshot())
    assert evidence["prepay_policy"] == "web_prepay_72h_v2"
    assert evidence["payment_elapsed_seconds"] == str(73 * 3600)
    assert evidence["payment_first_opened_msk"] == "20260904140000"
    assert evidence["site_created_msk"] == "20260828150000"


def test_payment_observation_revokes_an_approval_before_it_can_be_leased(db):
    policy.observe(db, snapshot(), apply_enabled=False, now=NOW)
    b = db.scalar(select(OrderClosureBatch))
    b.status = "approved"
    b.command_kind = "apply"
    result = policy.observe(db, snapshot(has_payment_history=True), apply_enabled=True, now=NOW)
    assert result.action == "manual_review" and b.command_kind is None
    assert b.last_error_code == "has_payment"
    assert queue.lease_commands(db, allow_auto_apply=True, now=NOW) == []
    # A later incorrect disappearance of payment history must not restart a failed apply.
    assert policy.observe(db, snapshot(), apply_enabled=True, now=NOW).action == "manual_review"


def test_pause_revokes_pending_approval_and_resume_requests_new_diagnosis(db):
    policy.observe(db, snapshot(), apply_enabled=False, now=NOW)
    b = db.scalar(select(OrderClosureBatch))
    b.status = "approved"
    b.command_kind = "apply"
    paused = clock((NOW - timedelta(hours=73), True), (NOW, False))
    policy.observe(db, snapshot(payment_clock=paused), apply_enabled=True, now=NOW)
    assert b.status == "draft" and b.command_kind is None
    assert queue.lease_commands(db, allow_auto_apply=True, now=NOW) == []
    resumed = clock(
        (NOW - timedelta(hours=73), True), (NOW, False), (NOW + timedelta(seconds=10), True)
    )
    later = NOW + timedelta(seconds=11)
    policy.observe(
        db, snapshot(payment_clock=resumed, observed_at=later), apply_enabled=True, now=later
    )
    assert b.status == "draft" and b.command_kind is None
    assert policy.pending_work(db)[0].action == "prepare_closure"
    assert b.diagnosis_hash is None


def test_expired_site_observation_is_checked_again_at_apply_leasing(db):
    policy.observe(db, snapshot(), apply_enabled=False, now=NOW)
    b = db.scalar(select(OrderClosureBatch))
    b.status = "approved"
    b.command_kind = "apply"
    assert queue.lease_commands(db, allow_auto_apply=True, now=NOW + timedelta(seconds=121)) == []
    assert b.status == "stale" and b.last_error_code == "site_snapshot_stale"


def test_conflict_during_a_live_lease_requires_manual_review_even_after_receipt(db):
    policy.observe(db, prepared_snapshot(), apply_enabled=False, now=NOW)
    b = db.scalar(select(OrderClosureBatch))
    b.status = "approved"
    b.command_kind = "apply"
    queue.lease_commands(db, allow_auto_apply=True, now=NOW)
    result = policy.observe(
        db, prepared_snapshot(has_payment_history=True), apply_enabled=True, now=NOW
    )
    assert result.action == "manual_review" and b.status == "leased"
    assert b.source_payload["requires_manual_review"]
    b.status = "applied"  # an in-flight native operation may have completed
    assert (
        policy.observe(db, prepared_snapshot(), apply_enabled=True, now=NOW).action
        == "manual_review"
    )
    assert policy.pending_work(db) == []


def test_discovery_waits_for_durable_site_fence(db):
    work = policy.observe(db, snapshot(), apply_enabled=True, now=NOW)
    assert work.action == "prepare_closure"
    assert queue.lease_commands(db, allow_auto_apply=True, now=NOW) == []
    policy.observe(db, prepared_snapshot(), apply_enabled=True, now=NOW)
    batch = db.scalar(select(OrderClosureBatch))
    assert batch.command_kind == "diagnose"
    xml = ElementTree.fromstring(queue.render_commands_xml([batch]))
    assert xml.findtext("command/order/payment_available") == "0"
    assert xml.findtext("command/order/payment_closure_hold_id") == str(UUID(int=99))


def test_unobserved_gap_cannot_extend_an_expired_site_grant():
    from app.services.order_prepay_clock import read_clock

    c = clock((NOW - timedelta(hours=73), True))
    c["valid_until"] = NOW - timedelta(hours=73) + timedelta(seconds=120)
    s = snapshot(payment_clock=c)
    assert read_clock(s).elapsed == timedelta(seconds=120)
    assert policy.site_blocker(s, NOW) == "payment_clock_paused"


def test_legacy_observations_are_not_authoritative_grants():
    c = clock((NOW - timedelta(hours=73), True))
    c.pop("source")
    assert policy.site_blocker(snapshot(payment_clock=c), NOW) == "payment_clock_source_unproven"


def test_started_payment_blocks_before_and_after_freeze(db):
    policy.observe(db, prepared_snapshot(), apply_enabled=True, now=NOW)
    batch = db.scalar(select(OrderClosureBatch))
    batch.status = "approved"
    batch.command_kind = "apply"
    result = policy.observe(
        db, prepared_snapshot(payment_started=True), apply_enabled=True, now=NOW
    )
    assert result.action == "manual_review"
    assert batch.last_error_code == "payment_in_flight_or_unresolved"
    assert queue.lease_commands(db, allow_auto_apply=True, now=NOW) == []


def test_hold_cannot_be_released_or_replaced_during_lease(db):
    policy.observe(db, prepared_snapshot(), apply_enabled=True, now=NOW)
    batch = db.scalar(select(OrderClosureBatch))
    batch.status = "approved"
    batch.command_kind = "apply"
    assert queue.lease_commands(db, allow_auto_apply=True, now=NOW) == [batch]
    result = policy.observe(db, prepared_snapshot(closure_hold=None), apply_enabled=True, now=NOW)
    assert result.action == "manual_review"
    assert batch.last_error_code == "payment_closure_hold_changed"


def test_approval_without_hold_cannot_be_applied_even_if_flags_enabled(db):
    policy.observe(db, snapshot(), apply_enabled=True, now=NOW)
    batch = db.scalar(select(OrderClosureBatch))
    batch.status = "approved"
    batch.command_kind = "apply"
    assert queue.lease_commands(db, allow_auto_apply=True, now=NOW) == []
    assert batch.last_error_code == "payment_closure_hold_missing"


def test_site_cannot_grant_an_unbounded_future_interval():
    c = clock((NOW - timedelta(hours=73), True))
    c["valid_until"] = NOW + timedelta(seconds=121)
    assert policy.site_blocker(snapshot(payment_clock=c), NOW) == "payment_clock_lease_too_long"


def test_payment_conflict_after_native_receipt_stops_site_cancellation(db):
    policy.observe(db, prepared_snapshot(), apply_enabled=True, now=NOW)
    batch = db.scalar(select(OrderClosureBatch))
    batch.status = "applied"
    result = policy.observe(
        db, prepared_snapshot(has_payment_history=True), apply_enabled=True, now=NOW
    )
    assert result.action == "manual_review"
    assert batch.last_error_code == "has_payment"
