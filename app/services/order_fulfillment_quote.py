"""Read-only quote engine. Accounting confirmation remains exclusively in 1C.

The first adapter is explicitly synthetic and cannot be enabled as production stock.
No DB, external service, payment or reservation writes are performed here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, model_validator

from app.schemas.order_fulfillment_quote import (
    FulfillmentQuoteRequest,
    FulfillmentQuoteResponse,
    QuotedAllocation,
    QuotedLine,
    QuoteLeg,
    QuoteModel,
    QuoteOption,
    SourceAllocation,
    StockIdentity,
)


class QuoteUnavailable(ValueError):
    """Stable reason code; no private inventory/configuration contents in errors."""


class Route(QuoteModel):
    route_key: str
    label: str
    departure_weekdays: list[int] = Field(min_length=1, max_length=7)
    departure_local_time: time
    cutoff_before_departure_minutes: int = Field(ge=0, le=1440)
    transport_minutes: int = Field(ge=0, le=10080)

    @model_validator(mode="after")
    def check_schedule(self):
        if any(day not in range(1, 8) for day in self.departure_weekdays):
            raise ValueError("invalid weekday")
        if self.departure_local_time.tzinfo is not None:
            raise ValueError("departure must be local time")
        return self


class CarrierSchedule(QuoteModel):
    weekdays: list[int] | None
    local_time: time | None

    @model_validator(mode="after")
    def check_schedule(self):
        if (self.weekdays is None) != (self.local_time is None):
            raise ValueError("partial carrier schedule")
        if self.weekdays is not None and (
            not self.weekdays or any(day not in range(1, 8) for day in self.weekdays)
        ):
            raise ValueError("invalid carrier weekdays")
        if self.local_time is not None and self.local_time.tzinfo is not None:
            raise ValueError("carrier time must be local")
        return self


class TimingProfile(QuoteModel):
    schema_version: Literal[2]
    profile: str
    status: Literal["test_quote_only"]
    environment: Literal["test"]
    production_eligible: Literal[False]
    schedule_confirmed: Literal[False]
    timezone: Literal["Europe/Moscow"]
    duration_unit: Literal["minutes"]
    display_label: str
    notes: str
    quote_validity_minutes: int = Field(ge=1, le=30)
    assembly_minutes: int = Field(ge=0, le=1440)
    receiving_minutes: int = Field(ge=0, le=1440)
    final_packing_minutes: int = Field(ge=0, le=1440)
    internal_routes: list[Route]
    carrier_handoff: dict[str, CarrierSchedule]

    @model_validator(mode="after")
    def unique_routes(self):
        if len({r.route_key for r in self.internal_routes}) != len(self.internal_routes):
            raise ValueError("duplicate route")
        if set(self.carrier_handoff) != {"cdek", "russian_post", "yandex_delivery"}:
            raise ValueError("carrier profile incomplete")
        return self


class Warehouse(QuoteModel):
    warehouse_id: str
    name: str
    city: str
    active: bool = True
    technical: bool = False
    pickup_point_id: str | None = None


class StockRow(StockIdentity):
    warehouse_id: str
    free_quantity: Decimal = Field(ge=0, max_digits=18, decimal_places=6)
    quantity_step: Decimal = Field(gt=0, max_digits=18, decimal_places=6)


class InventorySnapshot(QuoteModel):
    provenance: Literal["synthetic_fixture"]
    observed_at: AwareDatetime
    central_warehouse_id: str
    warehouses: list[Warehouse]
    stock: list[StockRow]

    @model_validator(mode="after")
    def consistent(self):
        ids = [w.warehouse_id for w in self.warehouses]
        pickups = [w.pickup_point_id for w in self.warehouses if w.pickup_point_id]
        if len(ids) != len(set(ids)) or len(pickups) != len(set(pickups)):
            raise ValueError("duplicate warehouse/point")
        central = next(
            (w for w in self.warehouses if w.warehouse_id == self.central_warehouse_id), None
        )
        if central is None or not central.active or central.technical or central.city != "moscow":
            raise ValueError("invalid central")
        keys = [(row.key(), row.warehouse_id) for row in self.stock]
        if len(keys) != len(set(keys)) or any(row.warehouse_id not in ids for row in self.stock):
            raise ValueError("invalid stock mapping")
        steps = {}
        for row in self.stock:
            if row.key() in steps and steps[row.key()] != row.quantity_step:
                raise ValueError("inconsistent unit step")
            steps[row.key()] = row.quantity_step
        return self


def load_test_inputs(
    profile_path: str, inventory_path: str
) -> tuple[TimingProfile, InventorySnapshot]:
    # Paths are administrator configuration, never request parameters. Reload per quote.
    try:
        profile = TimingProfile.model_validate_json(Path(profile_path).read_text(encoding="utf-8"))
        inventory = InventorySnapshot.model_validate_json(
            Path(inventory_path).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise QuoteUnavailable("test_configuration_invalid") from exc
    return profile, inventory


def next_departure(ready: datetime, days: list[int], departure: time, cutoff: int = 0) -> datetime:
    for offset in range(9):
        candidate = datetime.combine(ready.date() + timedelta(days=offset), departure, ready.tzinfo)
        if candidate.isoweekday() in days and ready <= candidate - timedelta(minutes=cutoff):
            return candidate
    raise QuoteUnavailable("route_schedule_unavailable")


def _path(
    source: Warehouse, target: Warehouse, central: Warehouse
) -> list[tuple[str, str, str]] | None:
    if source.warehouse_id == target.warehouse_id:
        return []
    if source.city == target.city:
        key = "spb_store_to_pickup" if source.city == "spb" else "local_store_to_pickup"
        if target.warehouse_id == central.warehouse_id:
            key = "moscow_stores_to_central"
        return [(source.warehouse_id, target.warehouse_id, key)]
    if source.city != "moscow":
        return None  # Regional -> Moscow/other cities not approved by business rules.
    legs = []
    if source.warehouse_id != central.warehouse_id:
        legs.append((source.warehouse_id, central.warehouse_id, "moscow_stores_to_central"))
    key = "central_to_spb_pickup" if target.city == "spb" else "central_to_regional_pickup"
    legs.append((central.warehouse_id, target.warehouse_id, key))
    return legs


def calculate_quote(
    request: FulfillmentQuoteRequest,
    profile: TimingProfile,
    inventory: InventorySnapshot,
    *,
    now: datetime,
) -> FulfillmentQuoteResponse:
    if now.tzinfo is None or now.utcoffset() is None:
        raise QuoteUnavailable("server_clock_invalid")
    age = (now - inventory.observed_at).total_seconds()
    if age < 0 or age > 180:
        raise QuoteUnavailable("inventory_not_fresh")
    now = now.astimezone(ZoneInfo(profile.timezone))
    deadline = now + timedelta(minutes=profile.quote_validity_minutes)
    warehouses = {w.warehouse_id: w for w in inventory.warehouses if w.active and not w.technical}
    central = warehouses[inventory.central_warehouse_id]
    target = central
    if request.delivery_method == "pickup":
        target = next(
            (w for w in warehouses.values() if w.pickup_point_id == request.pickup_point_id), None
        )
        if target is None:
            raise QuoteUnavailable("pickup_point_unavailable")
    routes = {r.route_key: r for r in profile.internal_routes}
    paths = {}
    for wid, warehouse in warehouses.items():
        path = _path(warehouse, target, central)
        if path is not None and all(key in routes for _, _, key in path):
            paths[wid] = path
    remaining = {(row.key(), row.warehouse_id): row.free_quantity for row in inventory.stock}
    step = {row.key(): row.quantity_step for row in inventory.stock}
    results = {}
    warnings = ["Расчёт не резервирует товар. Оплата пока недоступна: нужны подтверждения 1С."]
    # Honour explicit selections before recommending remaining stock for other rows.
    ordered = sorted(request.lines, key=lambda line: (line.sources is None, line.line_id))
    for line in ordered:
        unit_step = step.get(line.key())
        if unit_step is None or line.quantity % unit_step:
            raise QuoteUnavailable("stock_identity_or_quantity_invalid")
        options = [
            QuoteOption(
                warehouse_id=wid,
                warehouse_name=warehouse.name,
                available_quantity=remaining.get((line.key(), wid), Decimal(0)),
                needs_transfer=bool(paths[wid]),
                intercity=warehouse.city != target.city,
            )
            for wid, warehouse in warehouses.items()
            if wid in paths
        ]
        options.sort(
            key=lambda o: (
                o.warehouse_id != target.warehouse_id,
                warehouses[o.warehouse_id].city != target.city,
                len(paths[o.warehouse_id]),
                o.warehouse_id,
            )
        )
        allocations = line.sources
        if allocations is None:
            allocations = []
            needed = line.quantity
            for option in options:
                available = option.available_quantity
                available -= available % unit_step
                quantity = min(needed, available)
                if quantity > 0:
                    allocations.append(
                        SourceAllocation(warehouse_id=option.warehouse_id, quantity=quantity)
                    )
                    needed -= quantity
                if needed == 0:
                    break
            if needed:
                raise QuoteUnavailable("insufficient_stock")
        if sum((a.quantity for a in allocations), Decimal(0)) != line.quantity:
            raise QuoteUnavailable("allocation_quantity_mismatch")
        if len({a.warehouse_id for a in allocations}) != len(allocations):
            raise QuoteUnavailable("duplicate_source")
        quoted = []
        for allocation in allocations:
            wid = allocation.warehouse_id
            if wid not in paths:
                raise QuoteUnavailable("source_route_unavailable")
            key = (line.key(), wid)
            if allocation.quantity % unit_step:
                raise QuoteUnavailable("allocation_step_invalid")
            if allocation.quantity > remaining.get(key, Decimal(0)):
                raise QuoteUnavailable("selected_source_insufficient_stock")
            remaining[key] -= allocation.quantity
            ready = deadline + timedelta(minutes=profile.assembly_minutes)
            legs = []
            for source_id, target_id, route_key in paths[wid]:
                route = routes[route_key]
                departure = next_departure(
                    ready,
                    route.departure_weekdays,
                    route.departure_local_time,
                    route.cutoff_before_departure_minutes,
                )
                ready = departure + timedelta(
                    minutes=route.transport_minutes + profile.receiving_minutes
                )
                legs.append(
                    QuoteLeg(
                        source_warehouse_id=source_id,
                        target_warehouse_id=target_id,
                        departure_at=departure,
                        received_at=ready,
                    )
                )
            if warehouses[wid].city != target.city:
                warnings.append(
                    "Выбран межгород: срок включает обеспечение из Москвы через Центральный."
                )
            quoted.append(
                QuotedAllocation(
                    **allocation.model_dump(),
                    warehouse_name=warehouses[wid].name,
                    ready_at=ready,
                    legs=legs,
                )
            )
        results[line.line_id] = QuotedLine(
            **line.model_dump(exclude={"sources"}),
            sources=quoted,
            options=options,
        )
    lines = [results[line.line_id] for line in request.lines]
    ready = max(a.ready_at for line in lines for a in line.sources) + timedelta(
        minutes=profile.final_packing_minutes
    )
    handoff = None
    if request.delivery_method != "pickup":
        carrier = profile.carrier_handoff[request.delivery_method]
        if carrier.weekdays is not None and carrier.local_time is not None:
            handoff = next_departure(ready, carrier.weekdays, carrier.local_time)
        else:
            warnings.append(
                "Расписание передачи перевозчику не подтверждено; дата отправки неизвестна."
            )
        warnings.append(
            "Срок перевозчика до клиента не рассчитан и не входит в срок комплектования."
        )
    fingerprint = {
        "request": request.model_dump(mode="json"),
        "profile": profile.model_dump(mode="json"),
        "stock": inventory.model_dump(mode="json"),
        "deadline": deadline.isoformat(),
    }
    quote_id = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return FulfillmentQuoteResponse(
        quote_id=quote_id,
        quoted_at=now,
        payment_condition_until=deadline,
        ready_at=ready,
        carrier_handoff_at=handoff,
        consolidation_warehouse_id=target.warehouse_id,
        consolidation_warehouse_name=target.name,
        delivery_method=request.delivery_method,
        lines=lines,
        warnings=list(dict.fromkeys(warnings)),
        display_label=profile.display_label,
    )
