import type { CombineMessage } from "./types.ts";

/** A replayable broadcast: listeners can reconnect without stopping or duplicating the work. */
export function combineStream(work: (send: (message: CombineMessage) => void) => Promise<void>) {
  const encoder = new TextEncoder();
  const listeners = new Set<(chunk: Uint8Array | null) => void>();
  const history: CombineMessage[] = [];
  let finished = false;
  const send = (message: CombineMessage) => {
    history.push(message);
    // Keep the latest progress, terminal result and a useful tail of the engine log.
    if (history.length > 200) history.shift();
    const chunk = encoder.encode(JSON.stringify(message) + "\n");
    for (const listener of listeners) listener(chunk);
  };
  const subscribe = (initial?: CombineMessage) => {
    let heartbeat: ReturnType<typeof setInterval> | undefined;
    let listener: (chunk: Uint8Array | null) => void;
    const detach = () => {
      clearInterval(heartbeat);
      listeners.delete(listener);
    };
    return new ReadableStream<Uint8Array>({
      start(controller) {
        listener = (chunk) => {
          try {
            if (chunk === null) { detach(); controller.close(); }
            else controller.enqueue(chunk);
          } catch { detach(); } // Bun may close HTTP before delivering cancel().
        };
        if (initial) listener(encoder.encode(JSON.stringify(initial) + "\n"));
        for (const message of history) listener(encoder.encode(JSON.stringify(message) + "\n"));
        if (finished) listener(null);
        else {
          listeners.add(listener);
          // Blank NDJSON lines keep long CPU jobs connected without UI chatter.
          heartbeat = setInterval(() => listener(encoder.encode("\n")), 20_000);
        }
      },
      cancel: detach,
    });
  };
  const stream = subscribe();
  const completion = (async () => {
    // Reserve the job before invoking work, including synchronous failures.
    await Promise.resolve();
    try { await work(send); }
    catch (error) { send({ type: "error", message: String(error) }); }
    finally {
      finished = true;
      for (const listener of listeners) listener(null);
    }
  })();
  return { stream, subscribe, completion };
}
