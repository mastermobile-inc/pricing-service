import { useState } from "react";

export type RouteOption = { mode: "direct" | "via_transit"; warehouse_id: number; warehouse_name: string };
export type RoutedItem = { dropoff_warehouse_id?: number | null; final_warehouse_name?: string | null; route_options?: RouteOption[] };

export function DraftRouteControl({ item, disabled, onChange }: {
  item: RoutedItem; disabled: boolean; onChange: (mode: string) => void;
}) {
  const options = item.route_options || [];
  return <div className="logistics-route-fields">
    {item.final_warehouse_name && <small>Конечный магазин: {item.final_warehouse_name}</small>}
    {!!options.length && <label>Точка сдачи текущего рейса
      <select disabled={disabled} value={options.find(o => o.warehouse_id === item.dropoff_warehouse_id)?.mode || ""}
        onChange={e => onChange(e.target.value)}>
        {!options.some(o => o.warehouse_id === item.dropoff_warehouse_id) && <option value="">Проверьте маршрут</option>}
        {options.map(o => <option key={o.mode} value={o.mode}>
          {o.mode === "direct" ? "Напрямую" : "Через ЦС"}: {o.warehouse_name}
        </option>)}
      </select>
    </label>}
  </div>;
}

export type RerouteItem = { transfer_id: number; document_number: string; version?: number | null; route_options?: RouteOption[] };
export function LogisticsRerouteDialog({ item, submit, onClose, onDone }: {
  item: RerouteItem; submit: (payload: object) => Promise<unknown>; onClose: () => void; onDone: () => void;
}) {
  const [mode, setMode] = useState(item.route_options?.[0]?.mode || "direct");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // Stable for retries after an ambiguous network failure; changed inputs get a new key.
  const [key, setKey] = useState(() => crypto.randomUUID());
  const change = () => { setKey(crypto.randomUUID()); setError(""); };
  return <div className="logistics-camera" role="dialog" aria-modal="true" aria-label="Изменить точку сдачи">
    <div className="logistics-camera__sheet logistics-route-fields">
      <h3>Изменить точку сдачи: {item.document_number}</h3>
      <p>Это не приёмка. Новый склад должен отдельно отсканировать и принять документ.</p>
      <label>Новая точка<select value={mode} disabled={busy} onChange={e => { change(); setMode(e.target.value as typeof mode); }}>
        {item.route_options?.map(o => <option key={o.mode} value={o.mode}>{o.warehouse_name}</option>)}
      </select></label>
      <label>Причина<textarea value={reason} maxLength={1000} disabled={busy} onChange={e => { change(); setReason(e.target.value); }} /></label>
      {error && <p role="alert" className="logistics__message logistics__message--error">{error}</p>}
      <button className="btn" disabled={busy || !reason.trim() || !item.version} onClick={async () => {
        setBusy(true);
        try {
          await submit({ mode, reason: reason.trim(), expected_version: item.version, idempotency_key: key });
          onDone();
        } catch (err) {
          setError(err instanceof Error ? err.message : "Не удалось изменить маршрут. Обновите список");
        } finally { setBusy(false); }
      }}>Сохранить точку сдачи</button>
      <button className="btn btn--ghost" disabled={busy} onClick={onClose}>Закрыть и обновить список</button>
    </div>
  </div>;
}
