import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { SiteServiceRequestReturns } from "./SiteServiceRequestReturns";

vi.mock("../api/client", () => ({
  api: { get: vi.fn(), post: vi.fn() },
}));

vi.mock("react-hot-toast", () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

function shipment(overrides: Record<string, unknown> = {}) {
  return {
    id: 7,
    carrier: "cdek",
    tracking_number: "CDEK-7788",
    status: "arrived_at_pickup_point",
    status_changed_at: "2026-09-14T10:00:00+03:00",
    storage_deadline_at: "2026-09-21T12:00:00+03:00",
    arrived_at: "2026-09-14T09:40:00+03:00",
    picked_up_at: null,
    onec_return_confirmed_at: null,
    onec_order_ref: "236342",
    carrier_last_status_text: "Прибыло в пункт выдачи",
    ...overrides,
  };
}

describe("SiteServiceRequestReturns", () => {
  beforeEach(() => {
    vi.mocked(api.get).mockReset();
    vi.mocked(api.post).mockReset();
  });

  afterEach(() => cleanup());

  it("показывает состояние посылки и срок хранения", async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: { canRegister: true, returns: [shipment()] },
    } as never);

    render(<SiteServiceRequestReturns itemId={391} />);

    expect(await screen.findByText("Можно забирать")).toBeInTheDocument();
    expect(screen.getByText("CDEK-7788")).toBeInTheDocument();
    expect(screen.getByText("СДЭК", { selector: ".returns__carrier" })).toBeInTheDocument();
    expect(screen.getByText(/Хранение до/)).toBeInTheDocument();
    expect(screen.getByText("236342")).toBeInTheDocument();
  });

  it("оформляет возврат и показывает его в списке", async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: { canRegister: true, returns: [] },
    } as never);
    vi.mocked(api.post).mockResolvedValue({
      data: { canRegister: true, returns: [shipment({ status: "registered" })] },
    } as never);

    render(<SiteServiceRequestReturns itemId={391} />);

    expect(await screen.findByText("По этому обращению возвратов пока нет.")).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText("Номер, который прислал клиент"), {
      target: { value: "CDEK-7788" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Оформить возврат" }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/site-service-requests/ui/items/391/returns", {
        carrier: "cdek",
        trackingNumber: "CDEK-7788",
      }),
    );
    expect(await screen.findByText("Трек зарегистрирован")).toBeInTheDocument();
  });

  it("без прав на запись не показывает форму", async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: { canRegister: false, returns: [shipment()] },
    } as never);

    render(<SiteServiceRequestReturns itemId={391} />);

    expect(await screen.findByText("Можно забирать")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Оформить возврат" })).not.toBeInTheDocument();
    expect(screen.getByText(/нет прав заводить возвраты/i)).toBeInTheDocument();
  });

  it("предлагает повторить, если возвраты не загрузились", async () => {
    vi.mocked(api.get).mockRejectedValue(new Error("network"));

    render(<SiteServiceRequestReturns itemId={391} />);

    expect(
      await screen.findByText("Не удалось загрузить возвраты по обращению."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Повторить" })).toBeInTheDocument();
  });
});
