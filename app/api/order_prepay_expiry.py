from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Security
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.dependencies import _require_bearer_token, get_db, security
from app.core.config import get_settings
from app.schemas.order_prepay_expiry import (
    PrepayKnownResponse,
    PrepayTickRequest,
    PrepayWorkResponse,
)
from app.services import order_prepay_expiry as service
from app.services.order_closure import OrderClosureError

router = APIRouter(prefix="/api/order-closures/prepay72", tags=["order-closures"])


def site_access(credentials: HTTPAuthorizationCredentials | None = Security(security)) -> str:
    settings = get_settings()
    token = _require_bearer_token(
        credentials,
        settings.order_prepay72_site_token,
        missing_detail="prepay72 site token not configured",
    )
    if not settings.order_prepay72_enabled:
        raise HTTPException(status_code=503, detail="prepay72_disabled")
    return token


@router.get("/work", response_model=PrepayWorkResponse)
def work(_token: str = Depends(site_access), db: Session = Depends(get_db)):
    return PrepayWorkResponse(
        checked_at=datetime.now(UTC),
        apply_enabled=get_settings().order_prepay72_apply_enabled,
        items=service.pending_work(db),
    )


@router.post("/tick", response_model=PrepayWorkResponse)
def tick(
    payload: PrepayTickRequest, _token: str = Depends(site_access), db: Session = Depends(get_db)
):
    now = datetime.now(UTC)
    enabled = get_settings().order_prepay72_apply_enabled
    if len({x.site_order_id for x in payload.snapshots}) != len(payload.snapshots):
        raise HTTPException(status_code=422, detail="duplicate_site_order")
    try:
        items = [
            service.observe(db, snapshot, apply_enabled=enabled, now=now)
            for snapshot in payload.snapshots
        ]
        db.commit()
        return PrepayWorkResponse(checked_at=now, apply_enabled=enabled, items=items)
    except OrderClosureError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="prepay72_storage_unavailable") from exc


@router.get("/known", response_model=PrepayKnownResponse)
def known(
    cursor: int = Query(default=0, ge=0),
    _token: str = Depends(site_access),
    db: Session = Depends(get_db),
):
    from sqlalchemy import select

    from app.models.order_closure import OrderClosureBatch

    rows = db.scalars(
        select(OrderClosureBatch)
        .where(
            OrderClosureBatch.source_type == service.SOURCE,
            OrderClosureBatch.actor_id == service.ACTOR,
            OrderClosureBatch.id > cursor,
        )
        .order_by(OrderClosureBatch.id)
        .limit(1001)
    ).all()
    page = rows[:1000]
    return PrepayKnownResponse(
        site_order_ids=[x.source_payload["site_order_id"] for x in page],
        next_cursor=page[-1].id if len(rows) > 1000 else None,
    )
