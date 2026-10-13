import snapshot from "../../demo/follow/catalog.json";
import type { Catalog } from "./types.ts";

/** Map locations and measured phrases ship with the frontend; media is served separately. */
export const bundledCatalog = snapshot as unknown as Catalog;
const CACHE_KEY = "whales-catalog-snapshot";
const BUNDLE_VERSION = `${bundledCatalog.config_sha256}:${bundledCatalog.render_version}:${bundledCatalog.created_at_utc}`;

export function isCatalog(value: unknown): value is Catalog {
  if (!value || typeof value !== "object") return false;
  const catalog = value as Catalog;
  return Array.isArray(catalog.sources) && Array.isArray(catalog.points) && typeof catalog.countries === "string"
    && Boolean(catalog.combination && catalog.species) && typeof catalog.seed === "number"
    && catalog.sources.every((source) => source && typeof source.id === "string" && source.location
      && Number.isFinite(source.location.lon) && Number.isFinite(source.location.lat));
}

/** Ignore the export time and JSON property order; retain every actual catalog change. */
export function sameCatalog(a: Catalog, b: Catalog): boolean {
  const { created_at_utc: _a, ...first } = a;
  const { created_at_utc: _b, ...second } = b;
  return stable(first) === stable(second);
}

function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b))
    .map(([key, item]) => `${JSON.stringify(key)}:${stable(item)}`).join(",")}}`;
  return JSON.stringify(value);
}

/** A user-applied server update survives refreshes, until the frontend's bundled snapshot changes. */
export function initialCatalog(storage?: Pick<Storage, "getItem">): Catalog {
  try {
    const cached = JSON.parse(storage?.getItem(CACHE_KEY) ?? "null") as { bundle?: string; catalog?: unknown } | null;
    if (cached?.bundle === BUNDLE_VERSION && isCatalog(cached.catalog)) return cached.catalog;
  } catch { /* A blocked or damaged cache must not stop the map. */ }
  return bundledCatalog;
}

export function saveCatalog(storage: Pick<Storage, "setItem">, catalog: Catalog): void {
  storage.setItem(CACHE_KEY, JSON.stringify({ bundle: BUNDLE_VERSION, catalog }));
}
