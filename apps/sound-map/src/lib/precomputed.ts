import { buildSpec } from "./arrange.ts";
import { automaticCaption, defaultCaption, ensembleCaption, legacyCaption } from "./caption.ts";
import type { Catalog, ModelRegistry, PrecomputedIndex, Spec } from "./types.ts";

export function precomputedFiles(models: readonly string[]): string[] {
  return [...new Set(["duos.json", "trios.json", ...models.flatMap(model => [`duos-${model}.json`, `trios-${model}.json`])])];
}

/** Same defaults as the Python plan: automatic registers and the catalogue seed are resolved. */
export function specKey(spec: Spec, catalog: Catalog, model?: string, registry?: ModelRegistry): string {
  const sources = new Map(catalog.sources.map((s) => [s.id, s]));
  const parts = spec.parts.map((p) => ({ ...p, shift_octaves: p.shift_octaves ?? sources.get(p.source)?.register_shift_octaves ?? null }));
  const ace = spec.ace ? {
    task: spec.ace.task,
    caption: (spec.ace.caption || defaultCaption(spec.parts, catalog, model, registry)).slice(0, 400),
    audio_cover_strength: spec.ace.audio_cover_strength ?? catalog.combination.audio_cover_strength,
    seed: spec.ace.seed ?? catalog.seed,
  } : null;
  return JSON.stringify(buildSpec("", spec.arrangement, spec.gap_s, parts, ace));
}

export function precomputedPieces(index: PrecomputedIndex | null, catalog: Catalog): Map<string, string> {
  // Renderer changes invalidate generation caches in Python, but finished audio
  // stays playable when the recordings and composition settings are unchanged.
  if (!index || index.config_sha256 !== catalog.config_sha256) return new Map();
  return new Map(index.pieces.map((p) => [specKey(p.spec, catalog), p.id]));
}

/** Keep older default music playable without satisfying a request for the corrected prompt. */
export function earlierPieces(index: PrecomputedIndex | null, catalog: Catalog): Map<string, string> {
  if (!index || index.config_sha256 !== catalog.config_sha256) return new Map();
  return new Map(index.pieces.flatMap(({spec, id}) => {
    if (!spec.ace || !automaticCaption(spec, catalog)) return [];
    return [...new Set([legacyCaption(spec.parts, catalog), ensembleCaption(spec.parts, catalog)])].flatMap(caption => {
      const updated = { ...spec, ace: { ...spec.ace!, caption } };
      const key = specKey(updated, catalog);
      return key === specKey(spec, catalog) ? [] : [[key, id] as const];
    });
  }));
}
