export type CameraCapabilities = MediaTrackCapabilities & {
  zoom?: { min: number; max: number; step?: number };
  focusMode?: string[];
  focusDistance?: { min: number; max: number; step?: number };
};
export type CameraSettings = MediaTrackSettings & { zoom?: number; focusDistance?: number };
type Detector = { detect(image: HTMLVideoElement | HTMLCanvasElement): Promise<Array<{ rawValue?: string }>> };

export function startLogisticsCamera(video: HTMLVideoElement, deviceId: string, callbacks: {
  code: (value: string) => void;
  ready: (track: MediaStreamTrack, cameras: MediaDeviceInfo[]) => void;
  error: (error: unknown) => void;
}) {
  let active = true;
  let stream: MediaStream | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
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
        try { detector = new DetectorClass({ formats: ["qr_code", "code_128", "code_39", "ean_13", "ean_8"] }); } catch { /* use local ZXing */ }
      }
      let reader: import("@zxing/browser").BrowserMultiFormatReader | undefined;
      const canvas = document.createElement("canvas");
      const context = canvas.getContext("2d", { willReadFrequently: true });
      const decode = async (frame: HTMLVideoElement | HTMLCanvasElement) => {
        if (detector) {
          try { const result = await detector.detect(frame); if (result[0]?.rawValue) return result[0].rawValue; } catch { /* local fallback */ }
        }
        if (!active) return;
        if (!reader) {
          const { BrowserMultiFormatReader } = await import("@zxing/browser");
          reader = new BrowserMultiFormatReader();
        }
        if (!active || !context) return;
        if (frame === video) {
          canvas.width = video.videoWidth;
          canvas.height = video.videoHeight;
          context.drawImage(video, 0, 0);
        }
        try { return reader.decodeFromCanvas(canvas).getText(); } catch { /* normal undecodable frame */ }
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
