import { prepareZXingModule, readBarcodes, type ReaderOptions } from "zxing-wasm/reader";
import zxingWasmUrl from "zxing-wasm/reader/zxing_reader.wasm?url";

export type CameraCapabilities = MediaTrackCapabilities & {
  zoom?: { min: number; max: number; step?: number };
  focusMode?: string[];
  focusDistance?: { min: number; max: number; step?: number };
};
export type CameraSettings = MediaTrackSettings & { zoom?: number; focusDistance?: number };
type Detector = { detect(image: HTMLVideoElement | HTMLCanvasElement): Promise<Array<{ rawValue?: string }>> };

const READER_OPTIONS: ReaderOptions = {
  formats: ["QRCode", "Code128", "Code39", "EAN13", "EAN8", "DataMatrix"],
  tryHarder: true,
  tryInvert: true,
  tryRotate: true,
  maxNumberOfSymbols: 1,
};

let modulePrepared = false;

// The wasm binary ships with the bundle: Bitrix24 mobile has no access to a CDN.
function prepareReader() {
  if (modulePrepared) return;
  modulePrepared = true;
  prepareZXingModule({
    overrides: {
      locateFile: (path: string, prefix: string) =>
        path.endsWith(".wasm") ? zxingWasmUrl : `${prefix}${path}`,
    },
  });
}

// Register the local path before anything can start loading the module.
prepareReader();

/** Read a printed code from a photo or a video frame with the full ZXing engine. */
export async function decodeBarcodeSource(source: Blob | ImageData): Promise<string> {
  prepareReader();
  const results = await readBarcodes(source, READER_OPTIONS);
  for (const result of results) {
    const text = result.text?.trim();
    if (result.isValid && text) return text;
  }
  return "";
}

export function startLogisticsCamera(video: HTMLVideoElement, deviceId: string, callbacks: {
  code: (value: string) => void;
  ready: (track: MediaStreamTrack, cameras: MediaDeviceInfo[]) => void;
  error: (error: unknown) => void;
  attempt?: (attempts: number) => void;
}) {
  let active = true;
  let stream: MediaStream | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let attempts = 0;
  const stop = () => {
    active = false;
    clearTimeout(timer);
    stream?.getTracks().forEach(track => track.stop());
    stream = undefined;
    video.srcObject = null;
  };
  const start = async () => {
    try {
      if (!navigator.mediaDevices?.getUserMedia) throw new Error("Камера недоступна в этом браузере. Используйте фото или внешний сканер");
      const base = deviceId ? { deviceId: { exact: deviceId } } : { facingMode: { ideal: "environment" } };
      let requested: MediaStream;
      try {
        requested = await navigator.mediaDevices.getUserMedia({ video: { ...base, width: { ideal: 1920 }, height: { ideal: 1080 } }, audio: false });
      } catch (error) {
        if (!active) return;
        if ((error as { name?: string }).name === "NotAllowedError") throw error;
        requested = await navigator.mediaDevices.getUserMedia({ video: base, audio: false });
      }
      if (!active) { requested.getTracks().forEach(track => track.stop()); return; }
      stream = requested;
      video.srcObject = stream;
      await video.play();
      if (!active) return;
      const track = stream.getVideoTracks()[0];
      const caps = track.getCapabilities?.() as CameraCapabilities | undefined;
      if (caps?.focusMode?.includes("continuous")) {
        await track.applyConstraints({ advanced: [{ focusMode: "continuous" } as MediaTrackConstraintSet] }).catch(() => undefined);
      }
      const cameras = await navigator.mediaDevices.enumerateDevices?.().catch(() => []) || [];
      if (!active) return;
      callbacks.ready(track, cameras.filter(d => d.kind === "videoinput"));
      let detector: Detector | undefined;
      const DetectorClass = (window as unknown as { BarcodeDetector?: new (options: object) => Detector }).BarcodeDetector;
      if (DetectorClass) {
        try { detector = new DetectorClass({ formats: ["qr_code", "code_128", "code_39", "ean_13", "ean_8"] }); } catch { /* use bundled ZXing */ }
      }
      const canvas = document.createElement("canvas");
      const context = canvas.getContext("2d", { willReadFrequently: true });
      const decode = async (frame: HTMLVideoElement | HTMLCanvasElement) => {
        // The platform detector is free on Android; iOS has none and goes straight to wasm.
        if (detector) {
          try { const result = await detector.detect(frame); if (result[0]?.rawValue) return result[0].rawValue; } catch { /* wasm fallback */ }
        }
        if (!active || !context) return;
        if (frame === video) {
          canvas.width = video.videoWidth;
          canvas.height = video.videoHeight;
          context.drawImage(video, 0, 0);
        }
        if (!canvas.width || !canvas.height) return;
        try {
          return await decodeBarcodeSource(context.getImageData(0, 0, canvas.width, canvas.height));
        } catch { /* normal undecodable frame */ }
      };
      const loop = async () => {
        if (!active) return;
        if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && video.videoWidth > 0) {
          let value = await decode(video);
          if (!active) return;
          if (!value && context) {
            const width = Math.round(video.videoWidth * 0.6);
            const height = Math.round(video.videoHeight * 0.6);
            canvas.width = width; canvas.height = height;
            context.drawImage(video, (video.videoWidth - width) / 2, (video.videoHeight - height) / 2, width, height, 0, 0, width, height);
            value = await decode(canvas);
          }
          if (active && value?.trim()) { callbacks.code(value.trim()); return; }
          // A silent scanner reads as a broken app, so the UI counts real attempts.
          if (active) callbacks.attempt?.(++attempts);
        }
        // Schedule only AFTER both decoders/frames finish: no overlapping loops.
        if (active) timer = setTimeout(() => void loop().catch(error => { if (active) callbacks.error(error); }), 150);
      };
      await loop();
    } catch (error) { if (active) { stop(); callbacks.error(error); } }
  };
  void start();
  return stop;
}
