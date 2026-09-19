"""Durable file outbox. A missing reply never turns into a new operation."""

import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID, uuid4
from xml.etree import ElementTree as ET

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.procurement_pricing import ProcurementPriceBatch, ProcurementPriceEvent
from app.services.procurement_pricing import event

SCHEMA = "procurement_pricing.v1"


def build_message(batch: ProcurementPriceBatch) -> bytes:
    if (
        batch.status not in {"approved", "transmitting"}
        or not batch.approved_at
        or not batch.message_id
    ):
        raise ValueError("Неутверждённый пакет нельзя отправить")
    root = ET.Element(
        "PricingMessage",
        {
            "schema": SCHEMA,
            "messageId": batch.message_id,
            "key": batch.request_key,
            "approvedBy": batch.owner,
            "approvedAt": batch.approved_at.isoformat(),
            "mode": "apply",
        },
    )
    for line in batch.lines:
        node = ET.SubElement(
            root,
            "Line",
            {
                "code": line.code,
                "type": line.price_type,
                "currency": line.currency,
                "new": line.new_price,
            },
        )
        if line.old_price is not None:
            node.set("old", line.old_price)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def atomic_write(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError("Файл операции уже существует с другим содержимым")
        return
    temporary = path.with_name(path.name + "." + str(uuid4()) + ".tmp")
    try:
        with temporary.open("xb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        # Link is an atomic no-replace publication; concurrent workers cannot overwrite.
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != content:
                raise ValueError("Конфликт параллельной публикации") from None
    finally:
        temporary.unlink(missing_ok=True)


def export_approved(db: Session, root: Path, *, apply_enabled: bool, read_prices) -> dict:
    if not apply_enabled:
        return {"exported": 0, "blocked": "Передача выключена"}
    exported = 0
    ids = db.scalars(
        select(ProcurementPriceBatch.id)
        .where(ProcurementPriceBatch.status == "approved")
        .order_by(ProcurementPriceBatch.id)
    ).all()
    for id in ids:
        batch = db.scalar(
            select(ProcurementPriceBatch).where(ProcurementPriceBatch.id == id).with_for_update()
        )
        if batch.status != "approved":
            db.rollback()
            continue
        current = read_prices(sorted({x.code for x in batch.lines}))
        conflict = any(
            (line.code, line.price_type) not in current
            or current[(line.code, line.price_type)]["price"]
            != (Decimal(line.old_price) if line.old_price is not None else None)
            or current[(line.code, line.price_type)]["currency"] != line.currency
            for line in batch.lines
        )
        if conflict:
            batch.status = "conflict"
            batch.error = "Цены в 1С изменились после утверждения"
            event(db, batch, "exchange", "conflict")
            db.commit()
            continue
        batch.message_id = str(uuid4())
        batch.status = "transmitting"
        event(db, batch, "exchange", "transmitting")
        db.commit()
    # Recover a crash between the durable claim and atomic file publication.
    # Once archived, never resend automatically: wait for/recover the result.
    for batch in db.scalars(
        select(ProcurementPriceBatch).where(ProcurementPriceBatch.status == "transmitting")
    ).all():
        message_id = str(UUID(batch.message_id))
        name = f"procurement_pricing_{message_id}.ready.xml"
        if any((root / "to_1c" / folder / name).exists() for folder in ("archive", "error")):
            continue
        if (root / "from_1c" / "new" / f"procurement_pricing_{message_id}.result.xml").exists():
            continue
        path = root / "to_1c" / "new" / name
        atomic_write(path, build_message(batch))
        exported += 1
    return {"exported": exported}


def accept_result(db: Session, raw: bytes) -> ProcurementPriceBatch:
    if len(raw) > 2_000_000 or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("Недопустимый XML результата")
    root = ET.fromstring(raw)
    if root.tag != "PricingResult" or root.get("schema") != SCHEMA:
        raise ValueError("Неверная схема результата")
    message_id = str(UUID(root.get("messageId", "")))
    batch = db.scalar(
        select(ProcurementPriceBatch)
        .where(ProcurementPriceBatch.message_id == message_id)
        .with_for_update()
    )
    if batch is None or root.get("key") != batch.request_key:
        raise ValueError("Результат не относится к известной операции")
    state = root.get("status")
    if state not in {"applied", "error", "conflict"}:
        raise ValueError("Неизвестный статус результата")
    terminal = batch.status in {"applied", "error", "conflict"}
    if terminal:
        if batch.status != state or (
            state == "applied" and batch.document_ref != root.get("documentRef")
        ):
            raise ValueError("Противоречивый повтор результата")
    if not terminal and batch.status != "transmitting":
        raise ValueError("Пакет не ожидает результата")
    if state == "applied":
        if (
            not root.get("documentRef")
            or not root.get("documentNumber")
            or root.get("posted") != "true"
        ):
            raise ValueError("Нет подтверждения проведения документа")
        observed = {}
        for node in root.findall("Price"):
            key = (node.get("code"), node.get("type"))
            if key in observed:
                raise ValueError("Повтор readback строки")
            try:
                value = Decimal(node.get("value", "NaN"))
            except InvalidOperation as exc:
                raise ValueError("Некорректная цена readback") from exc
            if not value.is_finite():
                raise ValueError("Некорректная цена readback")
            observed[key] = (value, node.get("currency"))
        for line in batch.lines:
            if observed.get((line.code, line.price_type)) != (
                Decimal(line.new_price),
                line.currency,
            ):
                raise ValueError("Фактические цены не совпадают с утверждёнными")
        if terminal and batch.document_number != root.get("documentNumber"):
            raise ValueError("Противоречивый номер документа")
        batch.document_ref = root.get("documentRef")
        batch.document_number = root.get("documentNumber")
    if terminal:
        return batch
    batch.status = state
    batch.error = root.get("message") if state != "applied" else None
    event(db, batch, "exchange", state, {"document": batch.document_number})
    db.flush()
    return batch


def import_results(db: Session, root: Path) -> dict:
    imported = 0
    errors = []
    for path in sorted((root / "from_1c" / "new").glob("procurement_pricing_*.result.xml")):
        try:
            if path.stat().st_size > 2_000_000:
                raise ValueError("Слишком большой файл")
            accept_result(db, path.read_bytes())
            db.commit()
            archive = root / "from_1c" / "archive" / path.name
            atomic_write(archive, path.read_bytes())
            path.unlink()
            imported += 1
        except (ValueError, ET.ParseError):
            db.rollback()
            errors.append(path.name)
    return {"imported": imported, "rejected_files": errors}


def transport_health(db: Session) -> dict:
    states = dict(
        db.execute(
            select(ProcurementPriceBatch.status, func.count()).group_by(
                ProcurementPriceBatch.status
            )
        ).all()
    )
    oldest = db.scalar(
        select(func.min(ProcurementPriceEvent.created_at))
        .join(ProcurementPriceBatch, ProcurementPriceBatch.id == ProcurementPriceEvent.batch_id)
        .where(
            ProcurementPriceBatch.status == "transmitting",
            ProcurementPriceEvent.kind == "transmitting",
        )
    )
    return {"states": states, "oldest_transmitting_at": oldest.isoformat() if oldest else None}
