"""Read-only 1C facts. Physical price fields verified using live Params/DBNames."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from threading import Lock
from time import monotonic
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db import get_onec_engine
from app.models.competitor_item import CompetitorItem
from app.models.competitor_item_match import CompetitorItemMatch, CompetitorItemMatchStatus
from app.models.product import Product
from app.schemas.procurement_pricing import (
    CompetitorOffer,
    PriceHistoryPoint,
    PricingFilter,
    PricingRow,
    PricingTable,
)
from app.services.procurement_order_metrics import (
    _normalize_currency,
)
from app.services.procurement_pricing import filter_rows, forecast, ratio

PRICE_CODES = {"РБ0000005": "bronze", "РБ0000011": "platinum"}
PRICE_SELECT = """
 SELECT RTRIM(p._Code) code, RTRIM(t._Code) price_code,
        r._Period price_at, r._Fld6964 price,
        RTRIM(c._Code) currency_code, c._Description currency_name
 FROM _Reference62 p
 CROSS JOIN _Reference87 t
 OUTER APPLY (
    SELECT TOP 1 r.* FROM _InfoRg6959 r
    WHERE r._Fld6961RRef=p._IDRRef AND r._Fld6960RRef=t._IDRRef
      AND r._Fld6962RRef=0x00000000000000000000000000000000
      AND r._Active=0x01 AND r._Period<=:at
    ORDER BY r._Period DESC, r._RecorderTRef DESC, r._RecorderRRef DESC, r._LineNo DESC
 ) r
 LEFT JOIN _Reference20 c ON c._IDRRef=COALESCE(r._Fld6963RRef,t._Fld1016RRef)
 WHERE p._Marked=0x00 AND t._Marked=0x00
   AND p._Code IN :codes AND t._Code IN ('РБ0000005','РБ0000011')
"""


def current_prices(codes: list[str], *, at: datetime | None = None) -> dict:
    result = {}
    at = at or datetime.now(ZoneInfo("Europe/Moscow")).replace(tzinfo=None)
    requested = set(codes)
    if not requested:
        return result
    # Large IN lists make this legacy SQL Server choose a very slow plan.
    # A complete indexed price read is ~3 seconds; filter its result locally.
    if len(requested) > 200:
        query = text(PRICE_SELECT.replace("p._Code IN :codes AND ", ""))
        params = {"at": at}
    else:
        query = text(PRICE_SELECT).bindparams(bindparam("codes", expanding=True))
        params = {"codes": sorted(requested), "at": at}
    with get_onec_engine().connect() as connection:
        for row in connection.execute(query, params).mappings():
            if row["code"] not in requested:
                continue
            result[(row["code"], PRICE_CODES[row["price_code"]])] = {
                "price": row["price"],
                "currency": _normalize_currency(row["currency_code"], row["currency_name"], ""),
                "date": row["price_at"],
            }
    return result


def price_history(code: str, start: date, end: date) -> list[PriceHistoryPoint]:
    query = text("""
      SELECT r._Period price_at, RTRIM(t._Code) price_code,r._Fld6964 price,
             RTRIM(c._Code) currency_code,c._Description currency_name
      FROM _InfoRg6959 r
      JOIN _Reference62 p ON p._IDRRef=r._Fld6961RRef
      JOIN _Reference87 t ON t._IDRRef=r._Fld6960RRef
      LEFT JOIN _Reference20 c ON c._IDRRef=r._Fld6963RRef
      WHERE p._Code=:code AND t._Code IN ('РБ0000005','РБ0000011')
        AND r._Active=0x01 AND r._Fld6962RRef=0x00000000000000000000000000000000
        AND r._Period>=:start AND r._Period<:end
      ORDER BY r._Period,r._RecorderRRef
    """)
    points = []
    for (_item_code, kind), value in current_prices(
        [code], at=datetime.combine(start, time.min)
    ).items():
        if value["price"] is not None:
            points.append(
                PriceHistoryPoint(
                    date=datetime.combine(start, time.min),
                    price_type=kind,
                    price=value["price"],
                    currency=value["currency"],
                )
            )
    with get_onec_engine().connect() as connection:
        for row in connection.execute(
            query,
            {
                "code": code,
                "start": datetime.combine(start, time.min),
                "end": datetime.combine(end + timedelta(days=1), time.min),
            },
        ).mappings():
            points.append(
                PriceHistoryPoint(
                    date=row["price_at"],
                    price_type=PRICE_CODES[row["price_code"]],
                    price=row["price"],
                    currency=_normalize_currency(row["currency_code"], row["currency_name"], ""),
                )
            )
    return points


def _period(codes, start, end):
    if end <= start:
        return {}, {}, {}
    # Aggregate the entire catalog once per period; never repeat scans per page/SKU.
    from app.services.procurement_order_metrics import DEFECT_REASON_SQL

    # Native storage metadata: _Fld4979 is line amount; _Fld4982 is unit price.
    statements = [
        """SELECT RTRIM(product._Code) code, SUM(sale_line._Fld4971) sales_qty,
           SUM(sale_line._Fld4979) sales_amount FROM _Document203 sale
           JOIN _Document203_VT4966 sale_line ON sale_line._Document203_IDRRef=sale._IDRRef
           JOIN _Reference62 product ON product._IDRRef=sale_line._Fld4974RRef
           WHERE sale._Marked=0x00 AND sale._Posted=0x01 AND sale_line._Fld4971>0
             AND sale._Date_Time>=:start AND sale._Date_Time<:end GROUP BY product._Code""",
        f"""SELECT RTRIM(product._Code) code, SUM(ABS(return_line._Fld1707)) return_amount,
           SUM(CASE WHEN {DEFECT_REASON_SQL} THEN ABS(return_line._Fld1701) ELSE 0 END) defect_return_qty
           FROM _Document109 customer_return
           JOIN _Document109_VT1698 return_line ON return_line._Document109_IDRRef=customer_return._IDRRef
           JOIN _Reference62 product ON product._IDRRef=return_line._Fld1700RRef
           LEFT JOIN _Reference8913 return_reason ON return_reason._IDRRef=return_line._Fld8914_RRRef
           WHERE customer_return._Marked=0x00 AND customer_return._Posted=0x01 AND return_line._Fld1701>0
             AND customer_return._Date_Time>=:start AND customer_return._Date_Time<:end GROUP BY product._Code""",
        """SELECT RTRIM(product._Code) code,SUM(cost._Fld7588) cost_amount
           FROM _AccumRg7580 cost JOIN _Reference62 product ON product._IDRRef=cost._Fld7581RRef
           WHERE cost._Active=0x01 AND cost._RecorderTRef IN (0x000000CB,0x0000006D)
             AND cost._Period>=:start AND cost._Period<:end GROUP BY product._Code""",
    ]
    with get_onec_engine().connect() as connection:
        return tuple(
            {
                r["code"]: dict(r)
                for r in connection.execute(
                    text(sql),
                    {
                        "start": datetime.combine(start, time.min),
                        "end": datetime.combine(end, time.min),
                    },
                ).mappings()
            }
            for sql in statements
        )


def _last_year(day: date) -> date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        return day.replace(year=day.year - 1, day=28)


def _load_table(
    db: Session, filters: PricingFilter, *, paginate=True, include_prices=True
) -> PricingTable:
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    today = now.date()
    if filters.start > today:
        raise ValueError("Период ещё не начался")
    products = db.scalars(
        select(Product).where(
            Product.is_active.is_(True),
            Product.is_marked_for_deletion.is_(False),
            Product.code_1c.is_not(None),
        )
    ).all()
    # Only accepted matches are displayed; suggestions are not market evidence.
    offers = {}
    query = (
        select(CompetitorItemMatch.product_id, CompetitorItem, CompetitorItem.competitor)
        .join(CompetitorItem, CompetitorItem.id == CompetitorItemMatch.competitor_item_id)
        .where(
            CompetitorItemMatch.status == CompetitorItemMatchStatus.ACCEPTED,
            CompetitorItem.is_active.is_(True),
        )
    )
    for product_id, item, name in db.execute(query):
        if item.price_opt is not None:
            offers.setdefault(product_id, []).append(
                CompetitorOffer(
                    name=name,
                    price=item.price_opt,
                    url=item.url,
                    collected_at=item.scraped_at,
                    currency=None,
                )
            )
    engine = get_onec_engine()
    with engine.connect() as connection:
        first_sale = connection.execute(
            text("SELECT MIN(_Date_Time) FROM _Document203 WHERE _Posted=0x01 AND _Marked=0x00")
        ).scalar()
    rows = []
    warnings = []
    actual_end = min(filters.end + timedelta(days=1), today + timedelta(days=1))
    verified_from = get_settings().procurement_pricing_history_complete_from
    complete = (
        verified_from is not None
        and verified_from <= filters.start
        and first_sale is not None
        and first_sale.date() <= filters.start
    )
    if not complete:
        warnings.append("Полнота истории за период не подтверждена; прогноз недоступен")
    codes = sorted({p.code_1c.strip() for p in products if p.code_1c and p.code_1c.strip()})
    prices = current_prices(codes) if include_prices else {}
    sales, returns, costs = _period(codes, filters.start, actual_end)
    completed, completed_returns, _ = _period(codes, filters.start, min(actual_end, today))
    days = (actual_end - filters.start).days
    previous, _, _ = _period(
        codes,
        filters.start - timedelta(days=(filters.end - filters.start).days + 1),
        filters.start
        - timedelta(days=(filters.end - filters.start).days + 1)
        + timedelta(days=days),
    )
    last_year, ly_returns, _ = _period(codes, _last_year(filters.start), _last_year(actual_end))
    for product in products:
        code = product.code_1c.strip()
        sale = sales.get(code, {})
        ret = returns.get(code, {})
        qty = Decimal(sale.get("sales_qty") or 0)
        amount = Decimal(sale.get("sales_amount") or 0) - Decimal(ret.get("return_amount") or 0)
        cost = costs.get(code, {}).get("cost_amount")
        defects = Decimal(ret.get("defect_return_qty") or 0)
        old_qty = Decimal(previous.get(code, {}).get("sales_qty") or 0)
        fqty, status = forecast(
            Decimal(completed.get(code, {}).get("sales_qty") or 0),
            filters.start,
            filters.end,
            today,
            complete,
        )
        famount, _ = forecast(
            Decimal(completed.get(code, {}).get("sales_amount") or 0)
            - Decimal(completed_returns.get(code, {}).get("return_amount") or 0),
            filters.start,
            filters.end,
            today,
            complete,
        )
        row = PricingRow(
            code=code,
            name=product.name,
            subject=product.subject_1c or product.subject,
            brand=product.brand,
            quality=product.quality_raw or product.quality,
            sales_qty=qty,
            sales_amount=amount,
            profitability=ratio(amount - Decimal(cost), amount) if cost is not None else None,
            defect_qty=defects,
            defect_pct=ratio(defects, qty),
            dynamics_pct=ratio(qty - old_qty, old_qty),
            forecast_qty=fqty,
            forecast_amount=famount,
            forecast_status=status,
            previous_year_qty=(
                Decimal(last_year.get(code, {}).get("sales_qty") or 0)
                if first_sale and first_sale.date() <= _last_year(filters.start)
                else None
            ),
            previous_year_amount=(
                Decimal(last_year.get(code, {}).get("sales_amount") or 0)
                - Decimal(ly_returns.get(code, {}).get("return_amount") or 0)
                if first_sale and first_sale.date() <= _last_year(filters.start)
                else None
            ),
            competitors=offers.get(product.id, []),
        )
        for kind in ("bronze", "platinum"):
            price = prices.get((code, kind), {})
            if price.get("currency") == "RUB":
                setattr(row, kind, price.get("price"))
        rows.append(row)
    selected = filter_rows(rows, filters)
    return PricingTable(
        items=selected[filters.offset : filters.offset + filters.limit] if paginate else selected,
        total=len(selected),
        start=filters.start,
        end=filters.end,
        observed_at=now,
        facts_through=min(filters.end, today),
        warnings=warnings,
        subjects=sorted({r.subject for r in rows if r.subject}),
        brands=sorted({r.brand for r in rows if r.brand}),
        qualities=sorted({r.quality for r in rows if r.quality}),
    )


# Short, bounded in-process snapshots avoid rereading 1C for every filter/page.
# Approval and dispatch always call current_prices directly, bypassing this cache.
_snapshot_cache = {}
_snapshot_lock = Lock()


def build_table(db: Session, filters: PricingFilter, *, paginate=True) -> PricingTable:
    key = (id(db.get_bind()), filters.start, filters.end)
    with _snapshot_lock:
        cached = _snapshot_cache.get(key)
        if cached is None or monotonic() - cached[0] > 60:
            snapshot = _load_table(
                db,
                PricingFilter(start=filters.start, end=filters.end),
                paginate=False,
                include_prices=False,
            )
            if len(_snapshot_cache) >= 4:
                del _snapshot_cache[min(_snapshot_cache, key=lambda k: _snapshot_cache[k][0])]
            _snapshot_cache[key] = (monotonic(), snapshot)
        else:
            snapshot = cached[1]
    # Facts can be filtered before reading prices: the default page needs 50 SKUs,
    # not 29k. Price sorting and full export deliberately read the complete selection.
    selected = filter_rows(snapshot.items, filters)
    price_sort = filters.sort in {"bronze", "platinum"}
    if paginate and not price_sort:
        selected = selected[filters.offset : filters.offset + filters.limit]
    prices = current_prices([row.code for row in selected]) if selected else {}
    enriched = []
    for row in selected:
        updates = {}
        for kind in ("bronze", "platinum"):
            price = prices.get((row.code, kind), {})
            updates[kind] = price.get("price") if price.get("currency") == "RUB" else None
        enriched.append(row.model_copy(update=updates))
    if price_sort:
        enriched = filter_rows(enriched, filters)
        if paginate:
            enriched = enriched[filters.offset : filters.offset + filters.limit]
    return snapshot.model_copy(
        update={
            "items": enriched,
            "total": len(filter_rows(snapshot.items, filters)),
        }
    )
