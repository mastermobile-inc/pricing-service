import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { LogisticsPendingPanel, type Pending } from "./LogisticsPendingPanel";

afterEach(cleanup);
const page: Pending = { total: 21, scanned_count: 8, remaining_count: 13,
  freshness: { rtu: { stale: false }, transfer: { stale: true } },
  drivers: [{ id: 1, full_name: "Бывший водитель доставки" }],
  items: [{ transfer_id: 1, document_number: "РТУ-1", dropoff_warehouse_name: "Магазин", in_draft: true }] };

it("показывает остаток, свежесть и запрашивает следующую страницу на сервере", async () => {
  const load = vi.fn().mockResolvedValue(page);
  render(<LogisticsPendingPanel operation="handoff" warehouseId={1} draftId={2} revision={1} load={load} />);
  expect(await screen.findByText(/На складе остаются/)).toHaveTextContent("13");
  expect(screen.getByText(/Перемещения: нет свежего/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Следующие" }));
  await waitFor(() => expect(load).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 20, limit: 20 }), expect.any(AbortSignal)));
});

it("фильтрует приёмку по водителю доставки и не называет остаток потерей", async () => {
  const load = vi.fn().mockResolvedValue(page);
  render(<LogisticsPendingPanel operation="receipt" warehouseId={2} draftId={3} revision={1} load={load} />);
  fireEvent.change(await screen.findByRole("combobox"), { target: { value: "1" } });
  await waitFor(() => expect(load).toHaveBeenLastCalledWith(expect.objectContaining({ driver_id: 1 }), expect.any(AbortSignal)));
  expect(await screen.findByText(/это не подтверждение потери/)).toBeVisible();
  // Filtered counts must not be presented as the total being confirmed.
  expect(screen.queryByText(/Принимаете 8/)).not.toBeInTheDocument();
});

it("не подменяет свежий остаток запоздавшим ответом", async () => {
  let resolveFirst!: (value: Pending) => void;
  const load = vi.fn().mockImplementationOnce(() => new Promise<Pending>(r => { resolveFirst = r; }))
    .mockResolvedValue({ ...page, total: 2, scanned_count: 1, remaining_count: 1 });
  const ui = render(<LogisticsPendingPanel operation="handoff" warehouseId={1} revision={1} load={load} />);
  ui.rerender(<LogisticsPendingPanel operation="handoff" warehouseId={1} revision={2} load={load} />);
  expect(await screen.findByText(/Всего по фильтру: 2 ·/)).toBeVisible();
  resolveFirst(page);
  await waitFor(() => expect(screen.queryByText(/Всего по фильтру: 21 ·/)).not.toBeInTheDocument());
});

it("даёт добавить документ из списка, когда камера не читает код", async () => {
  const waiting: Pending = { ...page, items: [
    { transfer_id: 1, document_number: "РТУ-1", dropoff_warehouse_name: "Магазин", in_draft: true, lookup_code: "MMLOG1|rtu|0x01" },
    { transfer_id: 2, document_number: "РТУ-2", dropoff_warehouse_name: "Магазин", in_draft: false, lookup_code: "MMLOG1|rtu|0x02" },
  ] };
  const onAdd = vi.fn();
  render(<LogisticsPendingPanel operation="handoff" warehouseId={1} draftId={2} revision={1}
    load={vi.fn().mockResolvedValue(waiting)} onAdd={onAdd} />);
  const add = await screen.findAllByRole("button", { name: "Добавить без сканирования" });
  // Only the document that is still missing from the draft can be added.
  expect(add).toHaveLength(1);
  fireEvent.click(add[0]);
  expect(onAdd).toHaveBeenCalledWith("MMLOG1|rtu|0x02");
});
