from secrets import compare_digest

from fastapi import APIRouter, Depends, Header, HTTPException

from app.core.config import get_settings
from app.schemas.site_customer_prices import (
    SiteCustomerPriceRequest,
    SiteCustomerPriceResponse,
)
from app.services.site_customer_prices import resolve_site_customer_price


def require_site_price_token(x_site_price_token: str | None = Header(default=None)):
    expected = get_settings().site_customer_prices_internal_token
    if not expected:
        raise HTTPException(status_code=503, detail="Site price check is disabled")
    if not x_site_price_token or not compare_digest(x_site_price_token, expected):
        raise HTTPException(status_code=401, detail="Invalid site price credential")


router = APIRouter(dependencies=[Depends(require_site_price_token)])


@router.post("/resolve", response_model=SiteCustomerPriceResponse)
def resolve(payload: SiteCustomerPriceRequest):
    return resolve_site_customer_price(payload.user_id)
