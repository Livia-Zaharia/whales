import { describe, expect, test } from "bun:test";
import { buildSpec, draftFor } from "../src/lib/arrange.ts";
import { earlierPieces, precomputedPieces, specKey } from "../src/lib/precomputed.ts";
import { defaultCaption, ensembleCaption } from "../src/lib/caption.ts";
import type { Catalog, PrecomputedIndex } from "../src/lib/types.ts";

const catalog: Catalog = { ...await Bun.file(new URL("../demo/follow/catalog.json", import.meta.url)).json(), config_sha256: "test", render_version: 4 };
const [a, b] = catalog.sources;
const request = buildSpec("", "layer", 1, [draftFor(a!), draftFor(b!)], { task: "cover", audio_cover_strength: 0.6 });
const resolved = buildSpec("", "layer", 1, request.parts.map((p) => ({
  ...p, shift_octaves: catalog.sources.find((s) => s.id === p.source)!.register_shift_octaves,
})), { task: "cover", audio_cover_strength: 0.6, seed: catalog.seed, caption: defaultCaption(request.parts, catalog) });
const index: PrecomputedIndex = { config_sha256: "test", render_version: 4, pieces: [{ id: "123456abcdef", spec: resolved }] };

describe("precomputed playback lookup", () => {
  test("every bundled duo stays playable with its original prompt after renderer updates", async () => {
    const actualCatalog = await Bun.file(new URL("../demo/follow/catalog.json", import.meta.url)).json() as Catalog;
    const bundled = await Bun.file(new URL("../demo/follow/duos.json", import.meta.url)).json() as PrecomputedIndex;
    const pieces = precomputedPieces(bundled, actualCatalog);
    const updatedCatalog = { ...actualCatalog, render_version: (actualCatalog.render_version ?? 1) + 3 };
    const legacyPieces = precomputedPieces(bundled, updatedCatalog);
    const sources = new Map(actualCatalog.sources.map((s) => [s.id, s]));
    expect(pieces.size).toBe(actualCatalog.sources.length * (actualCatalog.sources.length - 1) / 2);
    expect(legacyPieces.size).toBe(pieces.size);
    for (const piece of bundled.pieces) {
      const parts = piece.spec.parts.map((p) => draftFor(sources.get(p.source)!));
      const defaults = buildSpec("", "layer", 1, parts, { task: "cover", audio_cover_strength: actualCatalog.combination.audio_cover_strength });
      expect(pieces.get(specKey(defaults, actualCatalog))).toBe(piece.id);
      expect(legacyPieces.get(specKey(defaults, updatedCatalog))).toBe(piece.id);
      expect(pieces.get(specKey({ ...defaults, parts: [...defaults.parts].reverse() }, actualCatalog))).toBe(piece.id);
    }
  });

  test("all legacy duos remain available as earlier versions without replacing corrected requests", async () => {
    const actualCatalog = await Bun.file(new URL("../demo/follow/catalog.json", import.meta.url)).json() as Catalog;
    const bundled = await Bun.file(new URL("../demo/follow/duos.json", import.meta.url)).json() as PrecomputedIndex;
    const saved = precomputedPieces(bundled, actualCatalog);
    const earlier = earlierPieces(bundled, actualCatalog);
    const sources = new Map(actualCatalog.sources.map(source => [source.id, source]));
    expect(earlier.size).toBe(190);
    for (const piece of bundled.pieces) {
      const defaults = buildSpec("", "layer", 1, piece.spec.parts.map(part => draftFor(sources.get(part.source)!)),
        {task: "cover", audio_cover_strength: actualCatalog.combination.audio_cover_strength});
      const key = specKey(defaults, actualCatalog, "stable-audio-3-small");
      expect(earlier.get(key)).toBe(piece.id);
      expect(saved.has(key)).toBe(false);
    }
    expect(earlierPieces({...bundled, config_sha256: "changed"}, actualCatalog).size).toBe(0);
    expect(earlierPieces(index, catalog).has(specKey(request, catalog))).toBe(false);
  });

  test("model-specific defaults separate prompts while explicit comparison captions share a key", () => {
    expect(specKey(request, catalog, "base")).toBe(specKey(request, catalog, "turbo"));
    expect(specKey(request, catalog, "stable-audio-3-small")).not.toBe(specKey(request, catalog, "base"));
    const shared = {...request, ace: {...request.ace!, caption: "Shared comparison prompt"}};
    expect(specKey(shared, catalog, "stable-audio-3-small")).toBe(specKey(shared, catalog, "base"));
    expect(specKey({...request, ace: {...request.ace!, caption: ensembleCaption(request.parts, catalog)}}, catalog))
      .toBe(specKey(request, catalog, "stable-audio-3-small"));
  });

  test("default requests find the saved pair in either click order", () => {
    const pieces = precomputedPieces(index, catalog);
    expect(pieces.get(specKey(request, catalog))).toBe("123456abcdef");
    expect(pieces.get(specKey({ ...request, parts: [...request.parts].reverse() }, catalog))).toBe("123456abcdef");
    expect(specKey({ ...request, title: "My renamed preset" }, catalog)).toBe(specKey(request, catalog));
  });

  test("three selected recordings find a saved trio in every click order", () => {
    const sources = catalog.sources.slice(0, 3);
    const defaults = buildSpec("", "layer", 1, sources.map(draftFor), { task: "cover", audio_cover_strength: 0.6 });
    const trio: PrecomputedIndex = { ...index, pieces: [{ id: "fedcba654321", spec: defaults }] };
    const pieces = precomputedPieces(trio, catalog);
    for (const order of [[0, 1, 2], [0, 2, 1], [1, 0, 2], [1, 2, 0], [2, 0, 1], [2, 1, 0]]) {
      const request = { ...defaults, parts: order.map(i => defaults.parts[i]!) };
      expect(pieces.get(specKey(request, catalog))).toBe("fedcba654321");
    }
    expect(pieces.has(specKey(request, catalog))).toBe(false);
  });

  test("custom timing, balance, registers, captions and takes request their own renders", () => {
    const pieces = precomputedPieces(index, catalog);
    for (const change of [{ offset_s: 4 }, { gain_db: -3 }, { shift_octaves: a!.register_shift_octaves + 1 }, { trim_s: [0, 10] as [number, number] }]) {
      expect(pieces.has(specKey({ ...request, parts: [{ ...request.parts[0]!, ...change }, request.parts[1]!] }, catalog))).toBe(false);
    }
    expect(pieces.has(specKey({ ...request, ace: { ...request.ace!, seed: 7 } }, catalog))).toBe(false);
    expect(pieces.has(specKey({ ...request, ace: { ...request.ace!, caption: "A different composition" } }, catalog))).toBe(false);
    expect(pieces.has(specKey({ ...request, ace: null }, catalog))).toBe(false);
    expect(pieces.has(specKey({ ...request, arrangement: "sequence" }, catalog))).toBe(false);
  });

  test("changed recording configuration does not reuse bundled pieces", () => {
    expect(precomputedPieces({ ...index, config_sha256: "old" }, catalog).size).toBe(0);
    expect(precomputedPieces(null, catalog).size).toBe(0);
  });

  test("finished legacy duos remain ready after a renderer update", () => {
    const pieces = precomputedPieces({ ...index, render_version: 3 }, catalog);
    expect(pieces.get(specKey(request, catalog))).toBe("123456abcdef");
  });
});
