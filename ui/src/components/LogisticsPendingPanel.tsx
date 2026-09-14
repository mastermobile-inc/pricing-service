import { useEffect, useMemo, useState } from "react";
import { logisticsApi as api } from "../api/logistics";

export type Pending = {
  draft_total_count?: number;
  total: number; scanned_count: number; remaining_count: number;
  freshness: Record<string, { stale: boolean }>;
  drivers: Array<{ id: number; full_name: string }>;
  items: Array<{ transfer_id: number; document_number: string; dropoff_warehouse_name: string; in_draft: boolean; status_label?: string | null }>;
};

export type PendingLoader = (params: Record<string, string | number | undefined>, signal: AbortSignal) => Promise<Pending>;
const loadBitrix: PendingLoader = async (params, signal) => (await api.get<Pending>("/bitrix/logistics/pending-documents", { params, signal })).data;

export function LogisticsPendingPanel({ operation, warehouseId, draftId, revision, updating = false, load = loadBitrix }: {
  operation: "handoff" | "receipt"; warehouseId: number; draftId?: number; revision: unknown;
  load?: PendingLoader;
  updating?: boolean;
}) {
  const [offset, setOffset] = useState(0);
  const [driverId, setDriverId] = useState("");
  const [attempt, setAttempt] = useState(0);
  const request = useMemo(() => ({ operation, warehouseId, draftId, revision, offset, driverId, attempt, load, updating }),
    [operation, warehouseId, draftId, revision, offset, driverId, attempt, load, updating]);
  const [snapshot, setSnapshot] = useState<{ request: typeof request; page?: Pending; error?: string } | null>(null);
  const page = !updating && snapshot?.request === request ? snapshot.page : null;
  const error = !updating && snapshot?.request === request ? snapshot.error : "";
  useEffect(() => {
    const interval = window.setInterval(() => {
      if (document.visibilityState !== "hidden") setAttempt(n => n + 1);
    }, 60000);
    return () => window.clearInterval(interval);
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    if (request.updating) return () => controller.abort();
    request.load({ operation: request.operation, warehouse_id: request.warehouseId, draft_id: request.draftId, limit: 20, offset: request.offset,
        driver_id: request.driverId ? Number(request.driverId) : undefined }, controller.signal
    ).then((data) => { if (!controller.signal.aborted) {
      if (request.offset > 0 && request.offset >= data.total) setOffset(0);
      else setSnapshot({ request, page: data });
    } })
      .catch(() => { if (!controller.signal.aborted) setSnapshot({ request, error: "Не удалось обновить список. Он может быть неполным." }); });
    return () => controller.abort();
  }, [request]);
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
      <p>Всего по фильтру: {page.total} · Добавлено по фильтру: {page.scanned_count} · Осталось: {page.remaining_count}</p>
      {draftId && <p>Всего документов в черновике: {page.draft_total_count ?? page.scanned_count}</p>}
      {draftId && !driverId && <p>{handoff ? "Передаёте" : "Принимаете"} {page.scanned_count} документов.
        {handoff ? " На складе остаются" : " Ещё ожидаются"} {page.remaining_count}. Можно подтвердить часть.</p>}
      {!handoff && <p>Остаток — ещё не принятые документы, это не подтверждение потери груза.</p>}
      {Object.entries(page.freshness).filter(([, f]) => f.stale).map(([type]) =>
        <p key={type} role="status">{type === "rtu" ? "РТУ" : "Перемещения"}: нет свежего подтверждения синхронизации. Список может быть неполным.</p>)}
      {page.items.length === 0 && <p>На этой странице документов нет.</p>}
      <ul>{page.items.map(row => <li key={row.transfer_id}>
        {row.document_number} → {row.dropoff_warehouse_name} — {row.in_draft ? "В черновике" : "Ожидает сканирования"}
        {row.status_label && <small>{row.status_label}</small>}
      </li>)}</ul>
      <button className="btn btn--ghost" type="button" disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 20))}>Предыдущие</button>
      <button className="btn btn--ghost" type="button" disabled={offset + 20 >= page.total} onClick={() => setOffset(offset + 20)}>Следующие</button>
    </>}
  </section>;
}
