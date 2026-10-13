import { combineStream } from "./combine-stream.ts";
import type { CombineMessage, CompositionJob, Spec } from "./types.ts";

type Work = (send: (message: CombineMessage) => void, id: string) => Promise<void>;
export interface TrackedComposition {
  key: string;
  info: CompositionJob;
  stream: () => ReadableStream<Uint8Array>;
  completion: Promise<void>;
}

/** One GPU job, with recent outcomes retained for reloads and lost connections. */
export class CompositionJobs {
  private jobs = new Map<string, TrackedComposition>();
  private tail: Promise<void> = Promise.resolve();
  private reporters = new Map<string, (message: CombineMessage) => void>();

  get active(): TrackedComposition | null {
    return [...this.jobs.values()].find((job) => job.info.state === "running") ?? null;
  }

  get(id: string): TrackedComposition | undefined { return this.jobs.get(id); }

  pending(key: string): TrackedComposition | undefined {
    return [...this.jobs.values()].find((job) => job.key === key && (job.info.state === "running" || job.info.state === "queued"));
  }

  start(key: string, spec: Spec, work: Work): TrackedComposition {
    const pending = this.pending(key);
    if (pending) return pending;
    const previous = this.tail;
    const position = [...this.jobs.values()].filter((job) => job.info.state === "running" || job.info.state === "queued").length;
    const info: CompositionJob = { id: crypto.randomUUID(), spec, started_at_utc: new Date().toISOString(),
      state: position ? "queued" : "running", queue_position: position };
    const broadcast = combineStream(async (send) => {
      const report = (message: CombineMessage) => {
        if (message.type === "done" || message.type === "error") info.state = message.type;
        send(message);
      };
      this.reporters.set(info.id, report);
      if (position) report({ type: "progress", percent: null, stage: queueStage(position) });
      await previous;
      info.state = "running";
      info.queue_position = 0;
      report({ type: "job", job: { ...info } });
      report({ type: "progress", percent: null, stage: "Starting the composition engine…" });
      try {
        await work(report, info.id);
        if (info.state === "running") report({ type: "error", message: "The engine stopped without returning a finished piece." });
      } catch (error) {
        report({ type: "error", message: error instanceof Error ? error.message : String(error) });
      } finally {
        this.reporters.delete(info.id);
        let ahead = 0;
        for (const queued of this.jobs.values()) {
          if (queued.info.state !== "queued") continue;
          queued.info.queue_position = ahead++;
          this.reporters.get(queued.info.id)?.({ type: "job", job: { ...queued.info } });
          this.reporters.get(queued.info.id)?.({ type: "progress", percent: null, stage: queueStage(queued.info.queue_position) });
        }
      }
    });
    // Job listeners always receive metadata, even when the history tail has rolled over.
    void broadcast.stream.cancel();
    const job: TrackedComposition = { key, info, stream: () => broadcast.subscribe({ type: "job", job: { ...info } }), completion: broadcast.completion };
    this.jobs.set(info.id, job);
    this.tail = job.completion;
    const completed = [...this.jobs.values()].filter((item) => item.info.state === "done" || item.info.state === "error");
    for (const old of completed.slice(0, -20)) this.jobs.delete(old.info.id);
    return job;
  }
}

function queueStage(position: number): string {
  return position ? `Waiting to compose · ${position} composition${position === 1 ? "" : "s"} ahead` : "Your composition is next…";
}
