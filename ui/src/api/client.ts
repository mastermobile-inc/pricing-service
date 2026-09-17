import axios from "axios";
import type { InternalAxiosRequestConfig } from "axios";

const apiBase = import.meta.env.VITE_API_BASE_URL || "/api";

export const api = axios.create({
  baseURL: apiBase,
});

export function setApiAuthToken(token: string) {
  api.defaults.headers.common.Authorization = `Bearer ${token}`;
}

export function clearApiAuthToken() {
  delete api.defaults.headers.common.Authorization;
}

/** Сессия встроенной вкладки истекла и переподключиться не удалось. */
export class ApiSessionExpiredError extends Error {
  constructor() {
    super("Сессия вкладки истекла. Обновите страницу, чтобы продолжить.");
    this.name = "ApiSessionExpiredError";
  }
}

type Reauthorizer = () => Promise<void>;

let reauthorize: Reauthorizer | null = null;
let pendingReauth: Promise<void> | null = null;

/**
 * Регистрирует способ перевыпустить сессию вкладки.
 *
 * Пропуск встроенной вкладки живёт 15 минут. Без этого вкладка просто
 * продолжала стучаться с мёртвым пропуском: переписка показывалась пустой, а
 * отправка ответа падала с «Не удалось отправить ответ», хотя ничего не
 * сломалось и всё лечилось обновлением страницы.
 */
export function setApiReauthorizer(handler: Reauthorizer | null) {
  reauthorize = handler;
  pendingReauth = null;
}

type RetriedConfig = InternalAxiosRequestConfig & { __mmRetriedAfterAuth?: boolean };

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const status = error?.response?.status;
    const config = error?.config as RetriedConfig | undefined;
    if (status !== 401 || !config || config.__mmRetriedAfterAuth || !reauthorize) {
      return Promise.reject(error);
    }
    config.__mmRetriedAfterAuth = true;
    try {
      // Параллельные запросы (опрос переписки и отправка ответа) переподключаются
      // один раз на всех, иначе Bitrix24 получил бы пачку одинаковых запросов.
      if (!pendingReauth) {
        pendingReauth = reauthorize().finally(() => {
          pendingReauth = null;
        });
      }
      await pendingReauth;
    } catch {
      return Promise.reject(new ApiSessionExpiredError());
    }
    const refreshed = api.defaults.headers.common.Authorization;
    if (refreshed) {
      config.headers.Authorization = refreshed as string;
    }
    return api.request(config);
  },
);
