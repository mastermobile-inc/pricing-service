import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api/procurementPricing";
import { ProcurementPricing } from "./ProcurementPricing";
vi.mock("../api/procurementPricing", () => ({
  fetchPricing: vi.fn(),
  fetchPricePresets: vi.fn(),
  fetchPriceBatches: vi.fn(),
  fetchPriceHistory: vi.fn(),
  createPriceBatch: vi.fn(),
  approvePriceBatch: vi.fn(),
  savePricePreset: vi.fn(),
  deletePricePreset: vi.fn(),
  exportPricing: vi.fn(),
}));
const row: api.PricingRow = {
  code: "SKU1",
  name: "Дисплей A",
  subject: "Дисплей",
  brand: "Apple",
  quality: "Оригинал",
  bronze: "100",
  platinum: "80",
  currency: "RUB",
  sales_qty: "20",
  sales_amount: "2000",
  profitability: "30",
  defect_qty: "1",
  defect_pct: "5",
  dynamics_pct: "10",
  forecast_qty: "30",
  forecast_amount: "3000",
  forecast_status: "ready",
  previous_year_qty: "18",
  previous_year_amount: "1800",
  competitors: [],
};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.fetchPricing).mockResolvedValue({
    items: [row],
    total: 1,
    start: "2026-09-01",
    end: "2026-09-30",
    observed_at: "2026-09-19T12:00:00+03:00",
    warnings: [],
    subjects: ["Дисплей"],
    brands: ["Apple"],
    qualities: ["Оригинал"],
  });
  vi.mocked(api.fetchPricePresets).mockResolvedValue([]);
  vi.mocked(api.fetchPriceBatches).mockResolvedValue([]);
  vi.mocked(api.fetchPriceHistory).mockResolvedValue([]);
});
afterEach(cleanup);
describe("pricing 4065", () => {
  it("shows only the two editable tiers and approves a single changed tier", async () => {
    vi.mocked(api.createPriceBatch).mockResolvedValue({
      id: 7,
      status: "draft",
      version: 1,
      created_at: "2026-09-19",
      document_number: null,
      error: null,
      lines: [],
    });
    vi.mocked(api.approvePriceBatch).mockResolvedValue({
      id: 7,
      status: "approved",
      version: 2,
      created_at: "2026-09-19",
      document_number: null,
      error: null,
      lines: [],
    });
    render(<ProcurementPricing />);
    await screen.findByText("Дисплей A");
    expect(screen.queryByText("Серебро")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Новая Бронза SKU1"), {
      target: { value: "120" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Утвердить 1 изменений" }),
    );
    await waitFor(() =>
      expect(api.approvePriceBatch).toHaveBeenCalledWith(7, 1),
    );
    expect(api.createPriceBatch).toHaveBeenCalledWith(expect.any(String), [
      {
        code: "SKU1",
        price_type: "bronze",
        old_price: "100",
        new_price: "120",
        currency: "RUB",
      },
    ]);
  });
  it("does not approve zero and keeps the proposed price on conflict", async () => {
    render(<ProcurementPricing />);
    await screen.findByText("Дисплей A");
    fireEvent.change(screen.getByLabelText("Новая Бронза SKU1"), {
      target: { value: "0" },
    });
    expect(
      screen.getByRole("button", { name: "Утвердить 1 изменений" }),
    ).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Новая Бронза SKU1"), {
      target: { value: "120" },
    });
    vi.mocked(api.createPriceBatch).mockRejectedValue(
      new Error("Цена изменена"),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Утвердить 1 изменений" }),
    );
    await screen.findByRole("alert");
    expect(screen.getByLabelText("Новая Бронза SKU1")).toHaveValue(120);
    expect(api.approvePriceBatch).not.toHaveBeenCalled();
  });
  it("exports active filters and supports numeric profitability ranges", async () => {
    render(<ProcurementPricing />);
    await screen.findByText("Дисплей A");
    fireEvent.change(screen.getByLabelText("Рентабельность от, %"), {
      target: { value: "12" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Применить фильтры" }));
    await waitFor(() =>
      expect(api.fetchPricing).toHaveBeenLastCalledWith(
        expect.objectContaining({ profitability_min: "12" }),
        expect.anything(),
      ),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "CSV" })).not.toBeDisabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "CSV" }));
    await waitFor(() =>
      expect(api.exportPricing).toHaveBeenCalledWith(
        expect.objectContaining({ profitability_min: "12" }),
        "csv",
      ),
    );
  });
});
