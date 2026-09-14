import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useLogisticsScanQueue } from "./useLogisticsScanQueue";

afterEach(cleanup);
it("queues 20 fast inputs while another operation is pending without drops", async () => {
  const locked = { current: true };
  const process = vi.fn(async (code: string) => code);
  const success = vi.fn();
  const hook = renderHook(() => useLogisticsScanQueue({ process, success, failure: vi.fn(), locked }));
  act(() => { for (let i = 0; i < 20; i++) hook.result.current.enqueue(`qr${i}`); });
  expect(process).not.toHaveBeenCalled();
  locked.current = false;
  await waitFor(() => expect(hook.result.current.count).toBe(0));
  expect(process.mock.calls.map(call => call[0])).toEqual(Array.from({ length: 20 }, (_, i) => `qr${i}`));
  expect(success).toHaveBeenCalledTimes(20);
});

it("retains failed codes even if later queued scans succeed", async () => {
  const process = vi.fn().mockRejectedValueOnce(new Error("wrong warehouse")).mockResolvedValue("ok");
  const failure = vi.fn();
  const locked = { current: false };
  const hook = renderHook(() => useLogisticsScanQueue({ process, failure, success: vi.fn(), locked }));
  act(() => { hook.result.current.enqueue("bad"); hook.result.current.enqueue("good"); });
  await waitFor(() => expect(hook.result.current.count).toBe(0));
  expect(hook.result.current.failed).toEqual(["bad"]);
  expect(failure).toHaveBeenCalledWith("bad", expect.any(Error));
  act(() => hook.result.current.enqueue("bad"));
  await waitFor(() => expect(hook.result.current.failed).toEqual([]));
});
