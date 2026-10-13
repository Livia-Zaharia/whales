import appConfig from "../../../../resources/orchestra-app.json" with { type: "json" };
import type { Catalog, ModelRegistry, Spec } from "./types.ts";

/** Same configured template and sorted unique instrument names as Python. */
export function ensembleCaption(parts: Spec["parts"], catalog: Catalog): string {
  const sources = new Map(catalog.sources.map(source => [source.id, source]));
  const names: Record<string, string> = appConfig.music.instrument_prompt_names;
  const instruments = [...new Set(parts.flatMap(part => {
    const source = sources.get(part.source);
    if (!source) return [];
    const instrument = catalog.species[source.species].instrument;
    return [names[instrument] ?? instrument];
  }))].sort();
  return appConfig.music.caption_template.replace("{instruments}", instruments.join(", ")).slice(0, 400);
}

export function legacyCaption(parts: Spec["parts"], catalog: Catalog): string {
  const sources = new Map(catalog.sources.map(source => [source.id, source]));
  const species = new Set(parts.map(part => sources.get(part.source)?.species));
  const only = species.size === 1 ? [...species][0] : undefined;
  return (only ? catalog.species[only].caption : catalog.combination.caption).slice(0, 400);
}

/** The previous style caption suits ACE-Step; Stable Audio uses the selected instruments. */
export function defaultCaption(parts: Spec["parts"], catalog: Catalog, model = appConfig.music.default_model,
    registry: ModelRegistry = appConfig.music as ModelRegistry): string {
  const profiles = registry.caption_profiles ?? {};
  const engine = registry.models.find(choice => choice.id === model)?.engine;
  const profile = profiles[model] ?? (engine === "ace-step" ? "legacy" : "instruments");
  return profile === "legacy" ? legacyCaption(parts, catalog) : ensembleCaption(parts, catalog);
}

/** Earlier automatic captions remain identifiable; user-written captions stay intact. */
export function automaticCaption(spec: Spec, catalog: Catalog): boolean {
  return !spec.ace?.caption || spec.ace.caption === legacyCaption(spec.parts, catalog)
    || spec.ace.caption === ensembleCaption(spec.parts, catalog);
}
