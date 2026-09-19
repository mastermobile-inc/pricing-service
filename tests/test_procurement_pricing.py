from datetime import date
from decimal import Decimal
from uuid import uuid4
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.models.procurement_pricing import ProcurementPriceBatch,ProcurementPriceLine,ProcurementPriceEvent,ProcurementPricePreset
from app.schemas.procurement_pricing import PriceBatchCreate,PriceBatchRead,PricingFilter,PricingRow
from app.services.procurement_pricing import create_batch,approve_batch,get_batch,PricingConflict,filter_rows,forecast
from app.services.procurement_pricing_exchange import export_approved,accept_result,SCHEMA

@pytest.fixture
def db():
    engine=create_engine('sqlite://')
    for model in (ProcurementPriceBatch,ProcurementPriceLine,ProcurementPriceEvent,ProcurementPricePreset):model.__table__.create(engine)
    with Session(engine) as session:yield session
    engine.dispose()

def payload(**changes):
    return PriceBatchCreate(request_key=uuid4(),lines=[{'code':'РБ01','price_type':'bronze','old_price':'100','new_price':'110',**changes}])

def prices(codes):return {('РБ01','bronze'):{'price':Decimal('100'),'currency':'RUB'}}

def test_forecast_includes_zero_days_and_requires_complete_history():
    assert forecast(Decimal(18),date(2026,9,1),date(2026,9,30),date(2026,9,19),True)==(Decimal(30),'ready')
    assert forecast(Decimal(18),date(2026,9,1),date(2026,9,30),date(2026,9,19),False)==(None,'history_incomplete')
    assert forecast(Decimal(0),date(2026,9,19),date(2026,9,30),date(2026,9,19),True)==(None,'no_completed_days')

def test_filters_are_combined_unknown_is_not_zero_and_nulls_sort_last():
    rows=[PricingRow(code='a',name='A',profitability=10,defect_pct=2),PricingRow(code='b',name='B',profitability=None,defect_pct=0),PricingRow(code='c',name='C',profitability=30,defect_pct=20)]
    f=PricingFilter(start=date(2026,9,1),end=date(2026,9,30),profitability_min=0,defect_max=5)
    assert [r.code for r in filter_rows(rows,f)]==['a']
    f=PricingFilter(start=f.start,end=f.end,sort='profitability',descending=True)
    assert [r.code for r in filter_rows(rows,f)]==['c','a','b']

@pytest.mark.parametrize('change',[{'new_price':'0'},{'new_price':'NaN'},{'new_price':'1.123'},{'price_type':'silver'}])
def test_invalid_or_hidden_price_type_rejected(change):
    with pytest.raises(ValidationError):payload(**change)

def test_approve_is_owner_scoped_conflict_checked_and_retry_safe(db):
    p=payload();batch=create_batch(db,p,'buyer');db.commit()
    assert create_batch(db,p,'buyer').id==batch.id
    with pytest.raises(LookupError):get_batch(db,batch.id,'other')
    with pytest.raises(PricingConflict):approve_batch(db,batch.id,1,'buyer',lambda _: {('РБ01','bronze'):{'price':Decimal('105'),'currency':'RUB'}})
    batch=approve_batch(db,batch.id,1,'buyer',prices);db.commit()
    assert approve_batch(db,batch.id,1,'buyer',prices).status=='approved'
    assert PriceBatchRead.model_validate(batch).lines[0].new_price==110

def test_export_disabled_by_default_and_retries_share_same_message(db,tmp_path):
    batch=create_batch(db,payload(),'buyer');approve_batch(db,batch.id,1,'buyer',prices);db.commit()
    assert export_approved(db,tmp_path,apply_enabled=False,rules_verified=True,read_prices=prices)['exported']==0
    assert not list(tmp_path.rglob('*.xml'))
    export_approved(db,tmp_path,apply_enabled=True,rules_verified=True,read_prices=prices)
    path=next(tmp_path.rglob('*.ready.xml'));content=path.read_bytes()
    export_approved(db,tmp_path,apply_enabled=True,rules_verified=True,read_prices=prices)
    assert path.read_bytes()==content
    assert len(list(tmp_path.rglob('*.ready.xml')))==1

def test_applied_requires_document_posting_and_exact_readback(db,tmp_path):
    batch=create_batch(db,payload(),'buyer');approve_batch(db,batch.id,1,'buyer',prices);db.commit()
    export_approved(db,tmp_path,apply_enabled=True,rules_verified=True,read_prices=prices)
    xml=f'<PricingResult schema="{SCHEMA}" messageId="{batch.message_id}" key="{batch.request_key}" status="applied" documentRef="ref" documentNumber="001" posted="true" dependentPricesVerified="true"><Price code="РБ01" type="bronze" value="110" currency="RUB"/></PricingResult>'.encode()
    with pytest.raises(ValueError):accept_result(db,xml.replace(b'value="110"',b'value="109"'))
    assert batch.status=='transmitting'
    assert accept_result(db,xml).status=='applied';db.commit()
    assert accept_result(db,xml).document_number=='001'

def test_conflict_after_approval_never_emits_xml(db,tmp_path):
    batch=create_batch(db,payload(),'buyer');approve_batch(db,batch.id,1,'buyer',prices);db.commit()
    export_approved(db,tmp_path,apply_enabled=True,rules_verified=True,read_prices=lambda _: {})
    assert batch.status=='conflict'
    assert not list(tmp_path.rglob('*.xml'))
