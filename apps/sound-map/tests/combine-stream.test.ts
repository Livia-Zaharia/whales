import { expect, test } from "bun:test";
import { combineStream } from "../src/lib/combine-stream.ts";

test("composition messages finish with normal EOF", async () => {
  const job = combineStream(async (send) => {
    send({ type: "log", text: "Rendering" });
    send({ type: "log", text: "Saved" });
  });
  const messages = (await new Response(job.stream).text()).trim().split("\n").map((line) => JSON.parse(line));
  expect(messages).toEqual([{ type: "log", text: "Rendering" }, { type: "log", text: "Saved" }]);
  await job.completion;
});

test("a failed composition reports its error and finishes without an unhandled rejection", async () => {
  const job = combineStream(async () => { throw new Error("Rendering failed"); });
  const message = JSON.parse(await new Response(job.stream).text());
  expect(message).toEqual({ type: "error", message: "Error: Rendering failed" });
  await job.completion;
});

test("disconnecting keeps the composition running and safely discards later messages", async () => {
  let finish!: () => void;
  const pending = new Promise<void>((resolve) => { finish = resolve; });
  let completed = false;
  const job = combineStream(async (send) => {
    send({ type: "log", text: "Rendering" });
    await pending;
    send({ type: "log", text: "Saved after disconnect" });
    completed = true;
  });
  const reader = job.stream.getReader();
  expect((await reader.read()).done).toBe(false);
  await reader.cancel();
  expect(completed).toBe(false);
  finish();
  await job.completion;
  expect(completed).toBe(true);
});

test("a composition failure after disconnect cannot crash the server", async () => {
  let fail!: () => void;
  const pending = new Promise<void>((resolve) => { fail = resolve; });
  const job = combineStream(async () => {
    await pending;
    throw new Error("Rendering failed after disconnect");
  });
  await job.stream.cancel();
  fail();
  await job.completion;
});
