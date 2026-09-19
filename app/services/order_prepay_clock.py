from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.schemas.order_prepay_expiry import SitePrepaySnapshot

POLICY = "web_prepay_72h_v2"


class PaymentClockError(ValueError):
    pass


@dataclass(frozen=True)
class PaymentClockReading:
    elapsed: timedelta
    first_opened_at: datetime
    available: bool
    revision: int


def read_clock(snapshot: SitePrepaySnapshot) -> PaymentClockReading:
    """Replay complete server-side transitions through the site observation.

    No time after observed_at is credited, and no creation/reserve timestamp
    substitutes for a missing opening. Enrollment/history cannot be truncated.
    """
    clock = snapshot.payment_clock
    if clock is None:
        raise PaymentClockError("payment_clock_missing")
    if clock.source != "site_gate_v1":
        raise PaymentClockError("payment_clock_source_unproven")
    if clock.valid_until and clock.valid_until > snapshot.observed_at + timedelta(seconds=120):
        raise PaymentClockError("payment_clock_lease_too_long")
    if not snapshot.created_at <= clock.enrolled_at <= snapshot.observed_at:
        raise PaymentClockError("payment_clock_enrollment_invalid")
    available = False
    elapsed = timedelta(0)
    previous_at = clock.enrolled_at
    first_opened = None
    ids = set()
    for sequence, event in enumerate(clock.events, 1):
        if event.sequence != sequence or event.event_id in ids:
            raise PaymentClockError("payment_clock_history_incomplete")
        if not previous_at <= event.occurred_at <= snapshot.observed_at:
            raise PaymentClockError("payment_clock_event_time_invalid")
        # Consecutive same-state events are invalid: producers deduplicate them.
        if event.available == available:
            raise PaymentClockError("payment_clock_transition_invalid")
        if available:
            elapsed += event.occurred_at - previous_at
        elif first_opened is None:
            first_opened = event.occurred_at
        available = event.available
        previous_at = event.occurred_at
        ids.add(event.event_id)
    if available:
        if clock.valid_until is None or clock.valid_until < previous_at:
            raise PaymentClockError("payment_clock_lease_missing")
        elapsed += min(snapshot.observed_at, clock.valid_until) - previous_at
        available = snapshot.observed_at < clock.valid_until
    if first_opened is None:
        raise PaymentClockError("payment_clock_missing_opening")
    return PaymentClockReading(elapsed, first_opened, available, len(clock.events))


def check_extension(original: SitePrepaySnapshot, current: SitePrepaySnapshot) -> None:
    """A subsequent observation may append transitions, never replace history."""
    before, after = original.payment_clock, current.payment_clock
    if before is None or after is None:
        raise PaymentClockError("payment_clock_missing")
    if original.closure_hold is not None and current.closure_hold != original.closure_hold:
        raise PaymentClockError("payment_closure_hold_changed")
    if before.source != after.source:
        raise PaymentClockError("payment_clock_source_changed")
    if before.valid_until and after.valid_until and after.valid_until < before.valid_until:
        raise PaymentClockError("payment_clock_lease_reversed")
    if (before.enrollment_id, before.enrolled_at) != (after.enrollment_id, after.enrolled_at):
        raise PaymentClockError("payment_clock_identity_changed")
    if current.observed_at < original.observed_at:
        raise PaymentClockError("payment_clock_observation_reversed")
    if after.events[: len(before.events)] != before.events:
        raise PaymentClockError("payment_clock_history_rewritten")
    if any(e.occurred_at < original.observed_at for e in after.events[len(before.events) :]):
        raise PaymentClockError("payment_clock_late_transition")
