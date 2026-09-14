"""Read-only 1C transfer ingestion. No physical movement or CRM side effects."""

from datetime import date

from sqlalchemy import String, bindparam, select, text
from sqlalchemy.exc import IntegrityError

from app.models import LogisticsTransfer, LogisticsWarehouse
from app.services import logistics
from app.services.logistics_onec import _bool_value


def fetch_page(
    engine,
    *,
    date_from: date | None,
    page_size=500,
    after=None,
    external_id=None,
    document_number=None,
):
    if not 1 <= page_size <= 5000:
        raise ValueError("page size must be between 1 and 5000")
    filters = []
    params = {}
    for name, value, condition in (
        (
            "date_from",
            date_from.strftime("%Y%m%d") if date_from else None,
            "d._Date_Time >= CONVERT(datetime, :date_from, 112)",
        ),
        ("after", after, "d._IDRRef > CONVERT(binary(16), :after, 1)"),
        ("external_id", external_id, "d._IDRRef = CONVERT(binary(16), :external_id, 1)"),
        ("document_number", document_number, "d._Number = :document_number"),
    ):
        if value is not None:
            filters.append(condition)
            params[name] = value
    # Read failed gates as well, so a later unposting/readiness loss is observed.
    query = text(f"""
        SELECT TOP {int(page_size)}
            LOWER(CONVERT(varchar(34), d._IDRRef, 1)) AS external_id,
            d._Number AS document_number, d._Date_Time AS document_date,
            LOWER(CONVERT(varchar(34), src._IDRRef, 1)) AS source_warehouse_external_id,
            LOWER(CONVERT(varchar(34), dst._IDRRef, 1)) AS document_target_warehouse_external_id,
            LOWER(CONVERT(varchar(34), final._IDRRef, 1)) AS target_warehouse_external_id,
            src._Description AS source_warehouse_name,
            dst._Description AS document_target_warehouse_name,
            final._Description AS target_warehouse_name,
            dst._Code AS accounting_warehouse_code,
            d._Marked AS deleted, d._Posted AS posted, d._Fld8954 AS printed
        FROM dbo._Document178 AS d
        LEFT JOIN dbo._Reference80 AS src ON src._IDRRef = d._Fld3819RRef
        LEFT JOIN dbo._Reference80 AS dst ON dst._IDRRef = d._Fld3820RRef
        LEFT JOIN dbo._Reference68 AS dep ON dep._IDRRef = d._Fld3818RRef
        LEFT JOIN dbo._Reference80 AS final ON final._IDRRef = dep._Fld8919RRef
        WHERE {' AND '.join(filters) if filters else '1=1'}
        ORDER BY d._IDRRef
    """)
    # Production's TDS connection rejects the parameterized document query,
    # although SELECT with a scalar parameter works. Use SQLAlchemy's typed,
    # escaped literals (never interpolate the caller's values into SQL).
    query = query.bindparams(
        *[bindparam(key, type_=String(), literal_execute=True) for key in params]
    )
    with engine.connect() as connection:
        rows = [dict(row) for row in connection.execute(query, params).mappings()]
        if not rows:
            return rows
        # One history lookup per page, not a correlated scan of the large 1C
        # history register for each document before TOP/ORDER BY are applied.
        scan_params = {f"ref_{i}": row["external_id"] for i, row in enumerate(rows)}
        refs_sql = ", ".join(f"CONVERT(binary(16), :{name}, 1)" for name in scan_params)
        history_query = text(f"""
            SELECT DISTINCT LOWER(CONVERT(varchar(34), s._Fld9449_RRRef, 1)) AS external_id
            FROM dbo._InfoRg9448 AS s
            WHERE s._Fld9449_TYPE = 0x08 AND s._Fld9449_RTRef = 0x000000B2
              AND s._Fld9454 = N'Отсканирован'
              AND s._Fld9449_RRRef IN ({refs_sql})
        """).bindparams(
            *[bindparam(name, type_=String(), literal_execute=True) for name in scan_params]
        )
        scanned = {
            r["external_id"] for r in connection.execute(history_query, scan_params).mappings()
        }
        return [{**row, "scanned": row["external_id"] in scanned} for row in rows]


def reconcile_page(session, rows, *, apply=False, planned_warehouses=None):
    report = dict(
        read=len(rows),
        created=0,
        updated=0,
        skipped=0,
        conflicts=0,
        resolved=0,
        warehouses_created=0,
    )
    if planned_warehouses is None:
        planned_warehouses = set()
    warehouses = {w.external_id.lower(): w.id for w in session.scalars(select(LogisticsWarehouse))}
    refs = [r["external_id"] for r in rows]
    # Same lock order as confirmation/reroute. Refresh identity-map state after waiting.
    ids = (
        session.scalars(
            select(LogisticsTransfer.id)
            .where(
                LogisticsTransfer.source_document_type == "transfer",
                LogisticsTransfer.external_id.in_(refs),
            )
            .order_by(LogisticsTransfer.id)
            .with_for_update()
        ).all()
        if apply and refs
        else []
    )
    if ids:
        session.expire_all()
    existing = (
        {
            u.external_id: u
            for u in session.scalars(
                select(LogisticsTransfer).where(
                    LogisticsTransfer.source_document_type == "transfer",
                    LogisticsTransfer.external_id.in_(refs),
                )
            )
        }
        if refs
        else {}
    )
    pending = []
    for raw in rows:
        ref = raw["external_id"]
        unit = existing.get(ref)
        warehouse_roles = ("source_warehouse", "target_warehouse", "document_target_warehouse")
        mappings_available = all(
            raw.get(f"{role}_external_id") in warehouses
            or (
                logistics.normalize_mm_log_document_ref(raw.get(f"{role}_external_id") or "")
                and str(raw.get(f"{role}_name") or "").strip()
            )
            for role in warehouse_roles
        )
        source, target, accounting = (
            warehouses.get(raw.get(key) or "")
            for key in (
                "source_warehouse_external_id",
                "target_warehouse_external_id",
                "document_target_warehouse_external_id",
            )
        )
        ready = (
            _bool_value(raw.get("posted"))
            and not _bool_value(raw.get("deleted"))
            and _bool_value(raw.get("printed"))
            and _bool_value(raw.get("scanned"))
            and str(raw.get("accounting_warehouse_code") or "").strip() == "РБ0000027"
            and mappings_available
        )
        if unit is None and not ready:
            report["skipped"] += 1
            continue
        active = (
            unit is not None
            and unit.state is not None
            and unit.state.last_event_type != logistics.EVENT_SYNCED
        )
        changed_route = unit is not None and (
            unit.source_warehouse_id,
            unit.target_warehouse_id,
            unit.document_target_warehouse_id,
        ) != (source, target, accounting)
        if active and (not ready or changed_route):
            report["conflicts"] += 1
            if apply:
                if not ready:
                    unit.payload = {**(unit.payload or {}), "ready_for_handoff": False}
                logistics._manual_review_for_sync_conflict(
                    session,
                    row=unit,
                    item=raw,
                    reason="Перемещение изменено в 1С после начала движения; требуется сверка",
                )
            continue
        if unit is not None and not ready:
            payload = {**(unit.payload or {}), "ready_for_handoff": False}
            status = "posted" if _bool_value(raw.get("posted")) else "not_posted"
            if (
                payload != unit.payload
                or unit.onec_deleted != _bool_value(raw.get("deleted"))
                or unit.onec_status != status
            ):
                report["updated"] += 1
                if apply:
                    unit.payload = payload
                    unit.onec_deleted = _bool_value(raw.get("deleted"))
                    unit.onec_status = status
            continue
        for role in warehouse_roles:
            warehouse_ref = raw[f"{role}_external_id"]
            if warehouse_ref in warehouses:
                continue
            if warehouse_ref not in planned_warehouses:
                report["warehouses_created"] += 1
                planned_warehouses.add(warehouse_ref)
            if apply:
                # A reference is not an access grant. Existing activation and
                # pilot allowlists are never changed by document ingestion.
                warehouse = LogisticsWarehouse(
                    external_id=warehouse_ref,
                    name=str(raw[f"{role}_name"]).strip(),
                    kind="warehouse",
                    is_active=False,
                )
                try:
                    with session.begin_nested():
                        session.add(warehouse)
                        session.flush()
                except IntegrityError:
                    warehouse = session.scalar(
                        select(LogisticsWarehouse).where(
                            LogisticsWarehouse.external_id == warehouse_ref
                        )
                    )
                    if warehouse is None:
                        raise
                warehouses[warehouse_ref] = warehouse.id
        # Package barcodes are not unique. The canonical document QR is the identity.
        code = f"MMLOG1|transfer|{ref}"
        item = {
            k: raw[k]
            for k in (
                "external_id",
                "document_number",
                "document_date",
                "source_warehouse_external_id",
                "target_warehouse_external_id",
                "document_target_warehouse_external_id",
            )
        }
        item.update(
            source_document_type="transfer",
            barcode=code,
            lookup_code=code,
            status="posted",
            onec_deleted=False,
            payload={"ready_for_handoff": True},
        )
        # Keep existing linked history, canonical lookup and optional CRM link intact.
        if unit:
            item.update(
                barcode=unit.barcode,
                lookup_code=unit.lookup_code,
                site_order_number=unit.site_order_number,
                origin_order_external_id=unit.origin_order_external_id,
                final_recipient_name=unit.final_recipient_name,
                payload={**(unit.payload or {}), "ready_for_handoff": True},
            )
        pending.append(item)
        if unit is None:
            report["created"] += 1
        elif any(
            (
                unit.source_warehouse_id != source,
                unit.target_warehouse_id != target,
                unit.document_target_warehouse_id != accounting,
                unit.onec_status != "posted",
                unit.onec_deleted,
                unit.payload != item["payload"],
                unit.document_number != raw["document_number"],
                unit.document_date != raw["document_date"],
            )
        ):
            report["updated"] += 1
    if apply:
        # sync_units seeds location only; it never creates handed/accepted events.
        logistics.sync_units(session, pending)
        for item in pending:
            report["resolved"] += logistics.resolve_unknown_qr_reviews_for_source_document(
                session,
                source_document_type="transfer",
                external_id=item["external_id"],
                auto_resolved_by="transfer_background_sync",
            )
        session.commit()
    return report


def sync_all(
    session,
    engine,
    *,
    date_from=None,
    page_size=500,
    apply=False,
    external_id=None,
    document_number=None,
    fetch=fetch_page,
    on_page=None,
):
    total = dict(
        read=0,
        created=0,
        updated=0,
        skipped=0,
        conflicts=0,
        resolved=0,
        warehouses_created=0,
        pages=0,
        dry_run=not apply,
    )
    planned_warehouses = set()
    after = None
    while True:
        rows = fetch(
            engine,
            date_from=date_from,
            page_size=page_size,
            after=after,
            external_id=external_id,
            document_number=document_number,
        )
        refs = [row["external_id"] for row in rows]
        if refs != sorted(set(refs)) or (refs and after and refs[0] <= after):
            raise RuntimeError("Unstable transfer pagination; success marker must not advance")
        report = reconcile_page(session, rows, apply=apply, planned_warehouses=planned_warehouses)
        for key, value in report.items():
            total[key] += value
        total["pages"] += 1
        if on_page is not None:
            on_page({**report, "page": total["pages"]})
        if len(rows) < page_size:
            return total
        after = refs[-1]
