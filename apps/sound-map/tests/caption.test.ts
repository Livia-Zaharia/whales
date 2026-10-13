import { describe, expect, test } from "bun:test";
import { draftFor, buildSpec } from "../src/lib/arrange.ts";
import { automaticCaption, defaultCaption, ensembleCaption } from "../src/lib/caption.ts";
import { bundledModels, isModelRegistry } from "../src/lib/model-variant.ts";
import type { Catalog } from "../src/lib/types.ts";

const catalog = await Bun.file(new URL("../demo/follow/catalog.json", import.meta.url)).json() as Catalog;
const trio = ["minke-whale-niihau", "right-whale-stellwagen", "spinner-dolphin-palmyra"]
  .map(id => draftFor(catalog.sources.find(source => source.id === id)!));

describe("selected-instrument prompts", () => {
  test("invalid model/profile combinations are rejected", () => {
    expect(isModelRegistry({...bundledModels, caption_profiles: {base: "unknown"}})).toBe(false);
    expect(isModelRegistry({...bundledModels, caption_profiles: {"../missing": "legacy"}})).toBe(false);
    expect(isModelRegistry({...bundledModels, models: bundledModels.models.filter(model => model.id !== "turbo")})).toBe(true);
  });
  test("ACE retains the previous style prompt while Stable uses the selected instruments", () => {
    expect(defaultCaption(trio, catalog, "base")).toBe(catalog.combination.caption);
    expect(defaultCaption(trio, catalog, "turbo")).toBe(catalog.combination.caption);
    expect(defaultCaption(trio, catalog, "stable-audio-3-small")).toBe(ensembleCaption(trio, catalog));
    const solo = [draftFor(catalog.sources.find(source => source.species === "humpback")!)];
    expect(defaultCaption(solo, catalog, "base")).toBe(catalog.species.humpback.caption);
  });

  test("a server-supplied profile overrides the bundled choice", () => {
    const registry = {...bundledModels, caption_profiles: {base: "instruments" as const}};
    expect(defaultCaption(trio, catalog, "base", registry)).toBe(ensembleCaption(trio, catalog));
  });
  test("the bell, horn and theremin trio receives the same exact caption as Python", () => {
    expect(ensembleCaption(trio, catalog)).toBe("Slow, spacious ambient chamber music featuring bells, horn, theremin. Use only these instruments; preserve the guide's timing and melodic contours. Instrumental, no vocals.");
    expect(ensembleCaption([...trio].reverse(), catalog)).toBe(ensembleCaption(trio, catalog));
  });

  test("one species does not add the other instruments from its old style caption", () => {
    const source = catalog.sources.find(source => source.species === "humpback")!;
    const caption = ensembleCaption([draftFor(source), draftFor(source)], catalog);
    expect(caption).toContain("featuring bowed strings.");
    expect(caption).not.toContain("piano");
  });

  test("saved automatic captions can update while custom captions stay untouched", () => {
    const spec = buildSpec("", "layer", 1, trio, {task: "cover", audio_cover_strength: 0.6});
    expect(automaticCaption(spec, catalog)).toBe(true);
    expect(automaticCaption({...spec, ace: {...spec.ace!, caption: catalog.combination.caption}}, catalog)).toBe(true);
    expect(automaticCaption({...spec, ace: {...spec.ace!, caption: ensembleCaption(trio, catalog)}}, catalog)).toBe(true);
    expect(automaticCaption({...spec, ace: {...spec.ace!, caption: "Solo piano by request"}}, catalog)).toBe(false);
  });
});
