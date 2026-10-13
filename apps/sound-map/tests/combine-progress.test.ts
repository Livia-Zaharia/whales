import { expect, test } from "bun:test";
import { compositionFailure, compositionProgress } from "../src/lib/combine-progress.ts";
import { readComposition } from "../src/lib/combine-reader.ts";
import type { CombineMessage } from "../src/lib/types.ts";

test("only well-formed engine progress changes the bar, and 100% waits for the result", () => {
  expect(compositionProgress('COMPOSE_PROGRESS {"percent":63,"stage":"Generating music"}')).toEqual({ type: "progress", percent: 63, stage: "Generating music" });
  expect(compositionProgress('COMPOSE_PROGRESS {"percent":100,"stage":"Saving"}')?.percent).toBe(99);
  for (const line of ['ACE-Step: Loading', 'COMPOSE_PROGRESS broken', 'COMPOSE_PROGRESS {"percent":"50","stage":"Loading"}', 'COMPOSE_PROGRESS {"percent":50}'])
    expect(compositionProgress(line)).toBeNull();
});

test("the UI gets the original model failure instead of the parent subprocess failure", () => {
  expect(compositionFailure(["ERROR: ACE-Step: Not enough memory", "Traceback (most recent call last):", "subprocess.CalledProcessError: returned non-zero exit status 1."], 1))
    .toBe("ACE-Step: Not enough memory");
  expect(compositionFailure(["ValueError: Unknown recording"], 2)).toBe("ValueError: Unknown recording");
});

test("split messages, heartbeat lines and a terminal line without newline reach the UI", async () => {
  const encoder = new TextEncoder();
  const response = new Response(new ReadableStream({ start(controller) {
    for (const chunk of ['\n{"type":"pro', 'gress","percent":40,"stage":"Rendering"}\n\n', '{"type":"error","message":"Model failed"}']) controller.enqueue(encoder.encode(chunk));
    controller.close();
  } }));
  const received: CombineMessage[] = [];
  await readComposition(response, (message) => received.push(message));
  expect(received).toEqual([{ type: "progress", percent: 40, stage: "Rendering" }, { type: "error", message: "Model failed" }]);
});

test("a lost connection is distinguishable from a completed job", async () => {
  await expect(readComposition(new Response('{"type":"progress","percent":40,"stage":"Rendering"}\n'), () => {})).rejects.toThrow("connection ended");
});
