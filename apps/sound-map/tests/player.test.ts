import { expect, test } from "bun:test";
import { Player } from "../src/player.ts";

test("a shared recording ending early does not stop the longer music; seeking keeps one clock", async () => {
  const original = globalThis.AudioContext;
  const sources: { buffer: { duration: number }; onended: (() => void) | null; start: (...args: number[]) => void; stop: () => void; connect: () => { connect: () => void } }[] = [];
  class Context {
    state = "running";
    currentTime = 0;
    destination = {};
    createGain() { return { gain: { value: 0, setTargetAtTime() {} }, connect() {} }; }
    createBufferSource() {
      const source = { buffer: { duration: 0 }, onended: null, start() {}, stop() {}, connect() { return { connect() {} }; } };
      sources.push(source);
      return source;
    }
  }
  globalThis.AudioContext = Context as unknown as typeof AudioContext;
  try {
    const player = new Player();
    player.load = async url => ({ duration: url === "recording" ? 30 : 30.5 }) as AudioBuffer;
    await player.play([{ key: "recording", url: "recording", gain: 1 }, { key: "music", url: "music", gain: 1 }], 10, 30.5);
    expect(sources[0]?.onended).toBeNull();
    expect(sources[1]?.onended).toBeFunction();
    expect(player.playing).toBe(true);
    sources[1]?.onended?.();
    expect(player.playing).toBe(false);
    expect(player.time).toBe(0);
  } finally {
    globalThis.AudioContext = original;
  }
});
