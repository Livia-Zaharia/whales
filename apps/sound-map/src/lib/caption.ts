import appConfig from "../../../../resources/orchestra-app.json" with { type: "json" };
import type { Catalog, Spec } from "./types.ts";

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

/** Earlier automatic captions remain identifiable; user-written captions stay intact. */
export function automaticCaption(spec: Spec, catalog: Catalog): boolean {
  const sources = new Map(catalog.sources.map(source => [source.id, source]));
  const species = new Set(spec.parts.map(part => sources.get(part.source)?.species));
  const only = species.size === 1 ? [...species][0] : undefined;
  const legacy = only ? catalog.species[only].caption : catalog.combination.caption;
  return !spec.ace?.caption || spec.ace.caption === legacy || spec.ace.caption === ensembleCaption(spec.parts, catalog);
}
