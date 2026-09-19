import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { LogisticsWorkspace } from "./LogisticsWorkspace";

vi.mock("../api/client", () => ({
  api: { get: vi.fn(), post: vi.fn() },
}));

const bootstrap = {
  profile: {
    id: 1,
    full_name: "Отправитель",
    role: "sender",
    default_warehouse_id: 10,
    default_warehouse_name: "Центральный склад",
  },
  warehouses: [
    { id: 10, external_id: "central", name: "Центральный склад", kind: "central" },
    { id: 20, external_id: "teply-stan", name: "Тёплый Стан", kind: "store" },
  ],
  drivers: [{ id: 30, full_name: "Иван Водитель" }],
  capabilities: ["handoff", "monitor", "history"],
};

describe("LogisticsWorkspace", () => {
  beforeEach(() => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/bitrix/logistics/bootstrap") return { data: bootstrap };
      if (path === "/bitrix/logistics/monitor") return { data: [] };
      throw new Error(`unexpected GET ${path}`);
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("требует фактическое количество и показывает ожидание учёта 1С", async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => ({
      data: path.endsWith("bootstrap") ? { ...bootstrap, capabilities: ["receipt", "monitor"] } : [],
    }));
    const draft = { id: 41, draft_type: "receipt", status: "open", warehouse_id: 10,
      driver_id: null, item_count: 0, items: [] };
    vi.mocked(api.post)
      .mockResolvedValueOnce({ data: draft })
      .mockResolvedValueOnce({ data: { ...draft, item_count: 1, items: [{
        id: 51, transfer_id: 51, document_number: "Пакет-51", barcode: "PKG-51",
        requires_goods_count: true,
        goods: [{ line_key: "line-1", name: "Кабель", barcode: "ITEM-1", quantity: "2" }],
      }] } })
      .mockResolvedValueOnce({ data: { processed_count: 1,
        accounting: [{ receipt_status: "matched", accounting_status: "pending" }] } });
    render(<LogisticsWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "Начать сканирование" }));
    fireEvent.change(await screen.findByPlaceholderText("QR, штрихкод или номер"), { target: { value: "PKG-51" } });
    fireEvent.click(screen.getByRole("button", { name: "Добавить код" }));
    const quantity = await screen.findByLabelText("Принято: Кабель");
    expect(quantity).toHaveValue(null);
    fireEvent.click(screen.getByRole("button", { name: "Подтвердить (1)" }));
    expect(await screen.findByText("Укажите фактически принятое количество: Кабель")).toBeVisible();
    expect(api.post).toHaveBeenCalledTimes(2);
    fireEvent.change(quantity, { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "Подтвердить (1)" }));
    expect(await screen.findByText(/ожидается подтверждение учёта 1С/)).toBeVisible();
    expect(api.post).toHaveBeenLastCalledWith("/bitrix/logistics/receipts/draft/41/confirm", expect.objectContaining({
      receipts: [{ transfer_id: 51, damaged: false, lines: [{ line_key: "line-1", quantity: "2" }] }],
    }));
  });

  it("сохраняет пакет сканов после ошибки следующего кода", async () => {
    vi.mocked(api.post)
      .mockResolvedValueOnce({
        data: {
          id: 41,
          draft_type: "handoff",
          status: "open",
          warehouse_id: 10,
          driver_id: 30,
          item_count: 0,
          items: [],
        },
      })
      .mockResolvedValueOnce({
        data: {
          id: 41,
          draft_type: "handoff",
          status: "open",
          warehouse_id: 10,
          driver_id: 30,
          item_count: 1,
          items: [
            {
              id: 51,
              document_number: "РТУ-000051",
              lookup_code: "MMLOG1|rtu|51|216951",
              barcode: "BC-51",
            },
          ],
        },
      })
      .mockRejectedValueOnce(new Error("Код уже добавлен"));

    render(<LogisticsWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "Начать сканирование" }));
    const input = await screen.findByPlaceholderText("QR, штрихкод или номер");
    fireEvent.change(input, { target: { value: "MMLOG1|rtu|51|216951" } });
    fireEvent.click(screen.getByRole("button", { name: "Добавить код" }));

    expect(await screen.findByText("РТУ-000051")).toBeVisible();
    expect(screen.getByRole("button", { name: "Подтвердить (1)" })).toBeEnabled();

    fireEvent.change(input, { target: { value: "BAD-CODE" } });
    fireEvent.click(screen.getByRole("button", { name: "Добавить код" }));
    expect(await screen.findByText("Код уже добавлен")).toBeVisible();
    expect(screen.getByText("РТУ-000051")).toBeVisible();
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(3));
  });
});
