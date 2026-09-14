import { afterEach, expect, it, vi } from "vitest";
import { waitFor } from "@testing-library/react";
import { startLogisticsCamera } from "./logisticsCamera";

const decode = vi.hoisted(() => vi.fn());
vi.mock("@zxing/browser", () => ({ BrowserMultiFormatReader: class { decodeFromCanvas = decode; } }));
let stop: (() => void) | undefined;
afterEach(() => { stop?.(); vi.restoreAllMocks(); vi.unstubAllGlobals(); decode.mockReset(); });

function setup() {
  const track = { stop: vi.fn(), getCapabilities: () => ({ focusMode: ["continuous"] }), applyConstraints: vi.fn().mockResolvedValue(undefined) };
  const stream = { getTracks: () => [track], getVideoTracks: () => [track] };
  const getUserMedia = vi.fn().mockResolvedValue(stream);
  vi.stubGlobal("navigator", { mediaDevices: { getUserMedia, enumerateDevices: vi.fn().mockResolvedValue([]) } });
  const video = document.createElement("video");
  Object.defineProperties(video, { videoWidth: { value: 1920 }, videoHeight: { value: 1080 }, readyState: { value: 4 } });
  vi.spyOn(video, "play").mockResolvedValue();
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ drawImage: vi.fn() } as unknown as CanvasRenderingContext2D);
  return { video, track, getUserMedia };
}

it("uses high resolution then safe constraints fallback; local decoder also reads linear codes", async () => {
  const { video, getUserMedia } = setup();
  getUserMedia.mockRejectedValueOnce(Object.assign(new Error(), { name: "OverconstrainedError" }));
  decode.mockReturnValue({ getText: () => "EAN1234" });
  const code = vi.fn();
  stop = startLogisticsCamera(video, "", { code, ready: vi.fn(), error: vi.fn() });
  await waitFor(() => expect(code).toHaveBeenCalledWith("EAN1234"));
  expect(getUserMedia).toHaveBeenLastCalledWith({ video: { facingMode: { ideal: "environment" } }, audio: false });
});

it("serializes full frame and crop and ignores a late result after stop", async () => {
  const { video } = setup();
  let finish!: (value: Array<{ rawValue: string }>) => void;
  const detect = vi.fn().mockResolvedValueOnce([]).mockImplementation(() => new Promise(r => { finish = r; }));
  vi.stubGlobal("BarcodeDetector", class { detect = detect; });
  decode.mockImplementation(() => { throw new Error("no code"); });
  const code = vi.fn();
  stop = startLogisticsCamera(video, "", { code, ready: vi.fn(), error: vi.fn() });
  await waitFor(() => expect(detect).toHaveBeenCalledTimes(2));
  expect(detect.mock.calls[0][0]).toBe(video);
  expect(detect.mock.calls[1][0]).toBeInstanceOf(HTMLCanvasElement);
  await new Promise(resolve => setTimeout(resolve, 350));
  expect(detect).toHaveBeenCalledTimes(2);
  stop();
  finish([{ rawValue: "too-late" }]);
  await Promise.resolve();
  expect(code).not.toHaveBeenCalled();
});
