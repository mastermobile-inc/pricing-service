import { useEffect, useState } from "react";
import { logisticsApi as api } from "../api/logistics";

export type Pending = {
  total: number; scanned_count: number; remaining_count: number;
  freshness: Record<string, { stale: boolean }>;
  drivers: Array<{ id: number; full_name: string }>;
  items: Array<{ transfer_id: number; document_number: string; dropoff_warehouse_name: string; in_draft: boolean }>;
};

export type PendingLoader = (params: Record<string, string | number | undefined>, signal: AbortSignal) => Promise<Pending>;
const loadBitrix: PendingLoader = async (params, signal) => (await api.get<Pending>("/bitrix/logistics/pending-documents", { params, signal })).data;

export function LogisticsPendingPanel({ operation, warehouseId, draftId, revision, load = loadBitrix }: {
  operation: "handoff" | "receipt"; warehouseId: number; draftId?: number; revision: unknown;
  load?: PendingLoader;
}) {
  const [page, setPage] = useState<Pending | null>(null);
  const [offset, setOffset] = useState(0);
  const [driverId, setDriverId] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [error, setError] = useState("");
  useEffect(() => {
    const interval = window.setInterval(() => {
      if (document.visibilityState !== "hidden") setAttempt(n => n + 1);
    }, 60000);
    return () => window.clearInterval(interval);
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setPage(null);
    setError("");
    load({ operation, warehouse_id: warehouseId, draft_id: draftId, limit: 20, offset,
        driver_id: driverId ? Number(driverId) : undefined }, controller.signal
    ).then((data) => { if (!controller.signal.aborted) {
      if (offset > 0 && offset >= data.total) setOffset(0);
      else setPage(data);
    } })
      .catch(() => { if (!controller.signal.aborted) setError("Не удалось обновить список. Он может быть неполным."); });
    return () => controller.abort();
  }, [operation, warehouseId, draftId, revision, offset, driverId, attempt, load]);
  const handoff = operation === "handoff";
  return <section className="logistics-card" aria-label="Оставшиеся документы">
    <h3>{handoff ? "Готовы к передаче" : "Ещё ожидаются"}</h3>
    <button type="button" className="btn btn--ghost" onClick={() => setAttempt(n => n + 1)}>Обновить список</button>
    {error && <p role="alert">{error}</p>}
    {!page && !error && <p role="status">Обновляем остаток документов…</p>}
    {page && <>
      {operation === "receipt" && <label>Водитель доставки
        <select value={driverId} onChange={e => { setDriverId(e.target.value); setOffset(0); }}>
          <option value="">Все водители</option>
          {page.drivers.map(d => <option key={d.id} value={d.id}>{d.full_name}</option>)}
        </select>
      </label>}
      <p>Всего: {page.total} · Добавлено в черновик: {page.scanned_count} · Осталось: {page.remaining_count}</p>
      {draftId && !driverId && <p>{handoff ? "Передаёте" : "Принимаете"} {page.scanned_count} документов.
        {handoff ? " На складе остаются" : " Ещё ожидаются"} {page.remaining_count}. Можно подтвердить часть.</p>}
      {!handoff && <p>Остаток — ещё не принятые документы, это не подтверждение потери груза.</p>}
      {Object.entries(page.freshness).filter(([, f]) => f.stale).map(([type]) =>
        <p key={type} role="status">{type === "rtu" ? "РТУ" : "Перемещения"}: нет свежего подтверждения синхронизации. Список может быть неполным.</p>)}
      {page.items.length === 0 && <p>На этой странице документов нет.</p>}
      <ul>{page.items.map(row => <li key={row.transfer_id}>
        {row.document_number} → {row.dropoff_warehouse_name} — {row.in_draft ? "В черновике" : "Ожидает сканирования"}
      </li>)}</ul>
      <button className="btn btn--ghost" type="button" disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 20))}>Предыдущие</button>
      <button className="btn btn--ghost" type="button" disabled={offset + 20 >= page.total} onClick={() => setOffset(offset + 20)}>Следующие</button>
    </>}
  </section>;
}
