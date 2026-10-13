import type { CombineMessage } from "./types.ts";

/** Keep a composition running when its listener closes the page or loses the connection. */
export function combineStream(work: (send: (message: CombineMessage) => void) => Promise<void>) {
  const encoder = new TextEncoder();
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  let closed = false;
  let heartbeat: ReturnType<typeof setInterval> | undefined;
  const stopHeartbeat = () => {
    if (heartbeat !== undefined) clearInterval(heartbeat);
    heartbeat = undefined;
  };
  const enqueue = (chunk: Uint8Array) => {
    if (closed) return;
    try { controller.enqueue(chunk); }
    catch {
      closed = true;
      stopHeartbeat();
    }
  };
  const send = (message: CombineMessage) => {
    if (!closed) enqueue(encoder.encode(JSON.stringify(message) + "\n"));
  };
  const stream = new ReadableStream<Uint8Array>({
    start(target) {
      controller = target;
      // Blank NDJSON lines keep long CPU jobs connected without adding messages to the UI.
      heartbeat = setInterval(() => enqueue(encoder.encode("\n")), 20_000);
    },
    cancel() {
      closed = true;
      stopHeartbeat();
    },
  });
  const completion = (async () => {
    try { await work(send); }
    catch (error) { send({ type: "error", message: String(error) }); }
    finally {
      stopHeartbeat();
      if (!closed) {
        closed = true;
        // Bun can close the HTTP stream before delivering its cancellation callback.
        try { controller.close(); }
        catch { /* The listener has already disconnected. */ }
      }
    }
  })();
  return { stream, completion };
}
