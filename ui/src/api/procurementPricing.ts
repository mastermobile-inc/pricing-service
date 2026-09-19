import { api } from './client';
export type PriceType = 'bronze' | 'platinum';
export type DecimalValue = string | number | null;
export interface PricingFilters {
  start: string; end: string; search: string; subject: string; brand: string; quality: string;
  profitability_min?: string; profitability_max?: string; defect_min?: string; defect_max?: string;
  trend: 'all' | 'up' | 'down'; sort: string; descending: boolean; offset: number; limit: number;
}
export interface PricingOffer {name: string; price: string; currency: string | null; url: string | null; collected_at: string | null}
export interface PricingRow {
  code: string; name: string; subject: string | null; brand: string | null; quality: string | null;
  bronze: DecimalValue; platinum: DecimalValue; currency: string; sales_qty: DecimalValue; sales_amount: DecimalValue;
  profitability: DecimalValue; defect_qty: DecimalValue; defect_pct: DecimalValue; dynamics_pct: DecimalValue;
  forecast_qty: DecimalValue; forecast_amount: DecimalValue; forecast_status: string;
  previous_year_qty: DecimalValue; previous_year_amount: DecimalValue; competitors: PricingOffer[];
}
export interface PricingTable {items: PricingRow[]; total: number; start: string; end: string; observed_at: string; warnings: string[]; subjects: string[]; brands: string[]; qualities: string[]}
export interface PriceChange {code: string; price_type: PriceType; old_price: DecimalValue; new_price: string; currency: 'RUB'}
export interface PriceBatch {id: number; status: string; version: number; created_at: string; document_number: string | null; error: string | null; lines: PriceChange[]}
export interface PricePreset {id: number; name: string; filters: PricingFilters}
export interface PricePoint {date: string; price_type: PriceType; price: string; currency: string}
const root = '/procurement-order-formation/pricing';
function params(filters: PricingFilters) {return Object.fromEntries(Object.entries(filters).filter(([,v]) => v !== ''));}
export async function fetchPricing(filters: PricingFilters, signal?: AbortSignal) {return (await api.get<PricingTable>(`${root}/table`, {params: params(filters), signal})).data;}
export async function fetchPriceHistory(code: string, start: string, end: string) {return (await api.get<PricePoint[]>(`${root}/history/${encodeURIComponent(code)}`,{params:{start,end}})).data;}
export async function fetchPriceBatches() {return (await api.get<PriceBatch[]>(`${root}/batches`)).data;}
export async function createPriceBatch(request_key: string, lines: PriceChange[]) {return (await api.post<PriceBatch>(`${root}/batches`,{request_key,lines})).data;}
export async function approvePriceBatch(id: number, version: number) {return (await api.post<PriceBatch>(`${root}/batches/${id}/approve`,{version})).data;}
export async function fetchPricePresets() {return (await api.get<PricePreset[]>(`${root}/presets`)).data;}
export async function savePricePreset(name: string, filters: PricingFilters) {return (await api.post<PricePreset>(`${root}/presets`,{name,filters})).data;}
export async function deletePricePreset(id: number) {await api.delete(`${root}/presets/${id}`);}
export async function exportPricing(filters: PricingFilters, format: 'xlsx' | 'csv') {
  const response=await api.get(`${root}/export`,{params:{...params(filters),format},responseType:'blob'});
  const url=URL.createObjectURL(response.data); const a=document.createElement('a'); a.href=url; a.download=`pricing-${filters.start}-${filters.end}.${format}`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
