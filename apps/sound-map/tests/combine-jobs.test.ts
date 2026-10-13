import { expect, test } from "bun:test";
import { CompositionJobs } from "../src/lib/combine-jobs.ts";
import { readComposition } from "../src/lib/combine-reader.ts";
import type { CombineMessage, Manifest, Spec } from "../src/lib/types.ts";

const spec: Spec = { arrangement: "layer", gap_s: 1, parts: [], ace: null };
const manifest = { id: "123456abcdef" } as Manifest;
const gate = () => {
  let finish!: () => void;
  const promise = new Promise<void>((resolve) => { finish = resolve; });
  return { promise, finish };
};
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const messages = async (stream: ReadableStream<Uint8Array>) => {
  const out: CombineMessage[] = [];
  await readComposition(new Response(stream), (message) => out.push(message));
  return out;
};

test("matching simultaneous requests share work and replay current progress to every listener", async () => {
  const jobs = new CompositionJobs();
  const wait = gate();
  let runs = 0;
  const work = async (send: (message: CombineMessage) => void) => {
    runs++;
    send({ type: "progress", percent: 61, stage: "Generating music" });
    await wait.promise;
    send({ type: "done", manifest });
  };
  const first = jobs.start("same", spec, work);
  const second = jobs.start("same", spec, work);
  expect(first).toBe(second);
  const original = messages(first.stream());
  await tick();
  const reconnected = messages(second.stream());
  wait.finish();
  const [a, b] = await Promise.all([original, reconnected]);
  expect(runs).toBe(1);
  for (const events of [a, b]) {
    expect(events).toContainEqual({ type: "progress", percent: 61, stage: "Generating music" });
    expect(events.at(-1)).toEqual({ type: "done", manifest });
  }
  await first.completion;
  expect(jobs.active).toBeNull();
  expect((await messages(jobs.get(first.info.id)!.stream())).at(-1)).toEqual({ type: "done", manifest });
});

test("different users queue in order, queued duplicates share one job, and queue positions advance", async () => {
  const jobs = new CompositionJobs();
  const waits = [gate(), gate(), gate()];
  const order: number[] = [];
  let executing = 0;
  let maximum = 0;
  const start = (i: number) => jobs.start(`job-${i}`, spec, async (send) => {
    order.push(i);
    maximum = Math.max(maximum, ++executing);
    await waits[i]!.promise;
    executing--;
    send({ type: "done", manifest: { ...manifest, id: String(i) } });
  });
  const first = start(0), second = start(1), third = start(2);
  expect(start(1)).toBe(second);
  expect(second.info.queue_position).toBe(1);
  expect(third.info.queue_position).toBe(2);
  await tick();
  expect(order).toEqual([0]);
  const reader = second.stream().getReader();
  expect(new TextDecoder().decode((await reader.read()).value)).toContain('"state":"queued"');
  await reader.cancel();
  waits[0]!.finish();
  await first.completion;
  await tick();
  expect(order).toEqual([0, 1]);
  expect(second.info.state).toBe("running");
  expect(third.info.queue_position).toBe(1);
  waits[1]!.finish();
  await second.completion;
  await tick();
  expect(order).toEqual([0, 1, 2]);
  waits[2]!.finish();
  await third.completion;
  expect(maximum).toBe(1);
});

test("failure and disconnected listeners release the engine for the next user's job", async () => {
  const jobs = new CompositionJobs();
  const wait = gate();
  const first = jobs.start("bad", spec, async () => { await wait.promise; throw new Error("Model could not load"); });
  const second = jobs.start("good", spec, async (send) => { send({ type: "done", manifest }); });
  await first.stream().cancel();
  wait.finish();
  await second.completion;
  expect(first.info.state).toBe("error");
  expect(second.info.state).toBe("done");
  expect((await messages(first.stream())).at(-1)).toEqual({ type: "error", message: "Model could not load" });
  expect((await messages(second.stream())).at(-1)).toEqual({ type: "done", manifest });
});

test("missing terminal results are reported and recent outcomes stay bounded", async () => {
  const jobs = new CompositionJobs();
  const first = jobs.start("empty", spec, async () => {});
  await first.completion;
  expect((await messages(first.stream())).at(-1)?.type).toBe("error");
  for (let i = 0; i < 22; i++) await jobs.start(String(i), spec, async (send) => send({ type: "done", manifest })).completion;
  expect(jobs.get(first.info.id)).toBeUndefined();
});
