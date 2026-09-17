import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ApiSessionExpiredError,
  api,
  clearApiAuthToken,
  setApiAuthToken,
  setApiReauthorizer,
} from "./client";

type Attempt = { status: number; authorization?: unknown };

/** Подменяет транспорт axios: первый ответ — отказ, дальше — успех. */
function mockTransport(statuses: number[]) {
  const attempts: Attempt[] = [];
  const queue = [...statuses];
  api.defaults.adapter = async (config) => {
    const status = queue.length > 1 ? (queue.shift() as number) : queue[0];
    attempts.push({ status, authorization: config.headers?.Authorization });
    const response = {
      data: { ok: status === 200 },
      status,
      statusText: "",
      headers: {},
      config,
    };
    if (status >= 400) {
      const error = new Error(`http ${status}`) as Error & { response?: unknown; config?: unknown };
      error.response = response;
      error.config = config;
      throw error;
    }
    return response;
  };
  return attempts;
}

afterEach(() => {
  setApiReauthorizer(null);
  clearApiAuthToken();
  api.defaults.adapter = undefined;
});

describe("переподключение вкладки", () => {
  it("перевыпускает пропуск и повторяет запрос с новым токеном", async () => {
    setApiAuthToken("stale");
    const attempts = mockTransport([401, 200]);
    const reauthorize = vi.fn(async () => {
      setApiAuthToken("fresh");
    });
    setApiReauthorizer(reauthorize);

    const { data } = await api.get("/anything");

    expect(data).toEqual({ ok: true });
    expect(reauthorize).toHaveBeenCalledTimes(1);
    expect(attempts.map((row) => row.authorization)).toEqual([
      "Bearer stale",
      "Bearer fresh",
    ]);
  });

  it("переподключается один раз на несколько параллельных запросов", async () => {
    setApiAuthToken("stale");
    mockTransport([401, 200]);
    const reauthorize = vi.fn(async () => {
      setApiAuthToken("fresh");
    });
    setApiReauthorizer(reauthorize);

    await Promise.all([api.get("/one"), api.get("/two"), api.get("/three")]);

    expect(reauthorize).toHaveBeenCalledTimes(1);
  });

  it("говорит про истёкшую сессию, если перевыпустить пропуск не вышло", async () => {
    setApiAuthToken("stale");
    mockTransport([401]);
    setApiReauthorizer(async () => {
      throw new Error("Bitrix24 SDK недоступен");
    });

    await expect(api.get("/anything")).rejects.toBeInstanceOf(ApiSessionExpiredError);
  });

  it("не зацикливается, когда свежий пропуск тоже отклонён", async () => {
    setApiAuthToken("stale");
    const attempts = mockTransport([401]);
    const reauthorize = vi.fn(async () => {
      setApiAuthToken("fresh");
    });
    setApiReauthorizer(reauthorize);

    await expect(api.get("/anything")).rejects.toThrow();
    expect(attempts).toHaveLength(2);
    expect(reauthorize).toHaveBeenCalledTimes(1);
  });

  it("другие ошибки остаются как были", async () => {
    mockTransport([500]);
    const reauthorize = vi.fn(async () => {});
    setApiReauthorizer(reauthorize);

    await expect(api.get("/anything")).rejects.toThrow();
    expect(reauthorize).not.toHaveBeenCalled();
  });
});
