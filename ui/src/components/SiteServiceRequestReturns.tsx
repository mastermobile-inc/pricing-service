import { useCallback, useEffect, useState } from "react";
import toast from "react-hot-toast";
import { api } from "../api/client";
import "./SiteServiceRequestReturns.css";

type ReturnCarrier = "russian_post" | "cdek";

type ReturnStatus =
  | "registered"
  | "in_transit"
  | "arrived_at_pickup_point"
  | "picked_up"
  | "onec_return_confirmed"
  | "cancelled"
  | "exception";

type ReturnShipment = {
  id: number;
  carrier: string;
  tracking_number: string;
  status: ReturnStatus;
  status_changed_at: string;
  storage_deadline_at: string | null;
  arrived_at: string | null;
  picked_up_at: string | null;
  onec_return_confirmed_at: string | null;
  onec_order_ref: string | null;
  carrier_last_status_text: string | null;
};

type ReturnsResponse = {
  canRegister: boolean;
  returns: ReturnShipment[];
};

const CARRIERS: { value: ReturnCarrier; label: string }[] = [
  { value: "cdek", label: "СДЭК" },
  { value: "russian_post", label: "Почта России" },
];

const carrierLabel: Record<string, string> = {
  cdek: "СДЭК",
  russian_post: "Почта России",
};

const statusLabel: Record<ReturnStatus, string> = {
  registered: "Трек зарегистрирован",
  in_transit: "В пути",
  arrived_at_pickup_point: "Можно забирать",
  picked_up: "Забрали",
  onec_return_confirmed: "Оформлен в 1С",
  cancelled: "Отменён",
  exception: "Проблема доставки",
};

const statusTone: Record<ReturnStatus, string> = {
  registered: "neutral",
  in_transit: "progress",
  arrived_at_pickup_point: "attention",
  picked_up: "done",
  onec_return_confirmed: "done",
  cancelled: "muted",
  exception: "alert",
};

const dateFormatter = new Intl.DateTimeFormat("ru-RU", {
  day: "2-digit",
  month: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

const dayFormatter = new Intl.DateTimeFormat("ru-RU", {
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
});

function formatDate(value: string | null) {
  return value ? dateFormatter.format(new Date(value)) : null;
}

function formatDay(value: string | null) {
  return value ? dayFormatter.format(new Date(value)) : null;
}

function daysLeft(value: string | null) {
  if (!value) return null;
  const diff = new Date(value).getTime() - Date.now();
  return Math.ceil(diff / (24 * 60 * 60 * 1000));
}

function errorMessage(error: unknown) {
  const detail =
    typeof error === "object" && error !== null
      ? ((error as { response?: { data?: { detail?: unknown } }; message?: string }).response?.data
          ?.detail ?? (error as { message?: string }).message)
      : null;
  const code = typeof detail === "string" ? detail : "";
  if (code.includes("another")) {
    return "Этот трек уже заведён на другое обращение или сделку.";
  }
  if (code === "ui_write_not_allowed") {
    return "Нет прав заводить возвраты. Обратитесь к руководителю.";
  }
  if (code === "service_request_lookup_unavailable") {
    return "Bitrix24 временно недоступен, попробуйте ещё раз.";
  }
  if (code.includes("tracking")) {
    return "Трек-номер не подходит выбранному перевозчику, проверьте номер.";
  }
  return "Не удалось оформить возврат.";
}

export function SiteServiceRequestReturns({ itemId }: { itemId: number }) {
  const [data, setData] = useState<ReturnsResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [carrier, setCarrier] = useState<ReturnCarrier>("cdek");
  const [tracking, setTracking] = useState("");
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await api.get<ReturnsResponse>(
        `/site-service-requests/ui/items/${itemId}/returns`,
      );
      setData(response.data);
      setLoadError(null);
    } catch {
      setLoadError("Не удалось загрузить возвраты по обращению.");
    }
  }, [itemId]);

  useEffect(() => {
    void load();
  }, [load]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const trimmed = tracking.trim();
    if (trimmed.length < 5) {
      toast.error("Введите трек-номер полностью.");
      return;
    }
    setSaving(true);
    try {
      const response = await api.post<ReturnsResponse>(
        `/site-service-requests/ui/items/${itemId}/returns`,
        { carrier, trackingNumber: trimmed },
      );
      setData(response.data);
      setTracking("");
      toast.success("Возврат зарегистрирован");
    } catch (error) {
      toast.error(errorMessage(error));
    } finally {
      setSaving(false);
    }
  };

  if (loadError) {
    return (
      <div className="returns returns--center">
        <p>{loadError}</p>
        <button type="button" onClick={() => void load()}>
          Повторить
        </button>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="returns returns--center">
        <p>Загружаем возвраты…</p>
      </div>
    );
  }

  return (
    <div className="returns">
      <header className="returns__header">
        <h1>Возврат товара</h1>
        <p>
          Возвраты по обращению ведутся в общем реестре логистики — здесь их видно и можно завести,
          не переходя в другой раздел.
        </p>
      </header>

      {data.returns.length === 0 ? (
        <p className="returns__empty">По этому обращению возвратов пока нет.</p>
      ) : (
        <ul className="returns__list">
          {data.returns.map((row) => {
            const left = daysLeft(row.storage_deadline_at);
            return (
              <li key={row.id} className="returns__item">
                <div className="returns__row">
                  <span className={`returns__status returns__status--${statusTone[row.status]}`}>
                    {statusLabel[row.status] || row.status}
                  </span>
                  <span className="returns__carrier">
                    {carrierLabel[row.carrier] || row.carrier}
                  </span>
                </div>
                <div className="returns__track">{row.tracking_number}</div>
                <dl className="returns__facts">
                  {row.storage_deadline_at && (
                    <>
                      <dt>Хранение до</dt>
                      <dd>
                        {formatDay(row.storage_deadline_at)}
                        {typeof left === "number" && left >= 0 && row.status !== "picked_up"
                          ? ` · осталось ${left} дн.`
                          : ""}
                      </dd>
                    </>
                  )}
                  {row.arrived_at && (
                    <>
                      <dt>Прибыл</dt>
                      <dd>{formatDate(row.arrived_at)}</dd>
                    </>
                  )}
                  {row.picked_up_at && (
                    <>
                      <dt>Забрали</dt>
                      <dd>{formatDate(row.picked_up_at)}</dd>
                    </>
                  )}
                  {row.onec_return_confirmed_at && (
                    <>
                      <dt>Оформлен в 1С</dt>
                      <dd>{formatDate(row.onec_return_confirmed_at)}</dd>
                    </>
                  )}
                  {row.onec_order_ref && (
                    <>
                      <dt>Заказ</dt>
                      <dd>{row.onec_order_ref}</dd>
                    </>
                  )}
                </dl>
                {row.carrier_last_status_text && (
                  <p className="returns__carrier-note">{row.carrier_last_status_text}</p>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {data.canRegister ? (
        <form className="returns__form" onSubmit={(event) => void submit(event)}>
          <h2>Оформить возврат</h2>
          <label>
            Перевозчик
            <select
              value={carrier}
              onChange={(event) => setCarrier(event.target.value as ReturnCarrier)}
              disabled={saving}
            >
              {CARRIERS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Трек-номер
            <input
              value={tracking}
              onChange={(event) => setTracking(event.target.value)}
              placeholder="Номер, который прислал клиент"
              disabled={saving}
            />
          </label>
          <button type="submit" disabled={saving || tracking.trim().length < 5}>
            {saving ? "Оформляем…" : "Оформить возврат"}
          </button>
          <p className="returns__hint">
            Трек попадёт в реестр логистики уже привязанным к этому обращению. Когда посылка
            приедет, состояние обновится здесь же.
          </p>
        </form>
      ) : (
        <p className="returns__hint">
          У вас нет прав заводить возвраты — обратитесь к руководителю онлайн-магазина.
        </p>
      )}
    </div>
  );
}
