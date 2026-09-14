import { useEffect, useRef, useState } from "react";

// HID scanners type quickly and finish with Enter. Never discard input just
// because another HTTP operation is pending. Confirmation is NOT queued here.
export function useLogisticsScanQueue<T>({ process, success, failure, locked }: {
  process: (code: string) => Promise<T>;
  success: (result: T) => void;
  failure: (code: string, error: unknown) => void;
  locked: { current: boolean };
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const callbacks = useRef({ process, success, failure });
  const queue = useRef<string[]>([]);
  const running = useRef(false);
  const mounted = useRef(true);
  const [count, setCount] = useState(0);
  const [failed, setFailed] = useState<string[]>([]);
  useEffect(() => { callbacks.current = { process, success, failure }; });
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; queue.current = []; };
  }, []);
  const drain = async () => {
    if (running.current) return;
    running.current = true;
    try {
      while (mounted.current && queue.current.length) {
        if (locked.current) {
          await new Promise(resolve => setTimeout(resolve, 20));
          continue;
        }
        locked.current = true;
        const code = queue.current.shift()!;
        try {
          const data = await callbacks.current.process(code);
          if (mounted.current) {
            setFailed(codes => codes.filter(value => value !== code));
            callbacks.current.success(data);
          }
        } catch (error) {
          if (mounted.current) {
            setFailed(codes => codes.includes(code) ? codes : [...codes, code]);
            callbacks.current.failure(code, error);
            // Highlight a retained failed code so the next HID scan replaces it.
            setTimeout(() => { if (mounted.current) inputRef.current?.select(); }, 0);
          }
        } finally {
          locked.current = false;
          if (mounted.current) {
            setCount(queue.current.length);
            inputRef.current?.focus();
          }
        }
      }
    } finally { running.current = false; }
  };
  return { inputRef, count, failed, enqueue: (code: string) => {
    if (!code.trim()) return;
    queue.current.push(code.trim());
    setCount(queue.current.length + (running.current ? 1 : 0));
    void drain();
  } };
}
