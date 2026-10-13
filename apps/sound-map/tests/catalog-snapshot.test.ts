import { expect, test } from "bun:test";
import { bundledCatalog, initialCatalog, isCatalog, sameCatalog, saveCatalog } from "../src/lib/catalog-snapshot.ts";
import geography from "../../../resources/maps/world-countries-110m.geojson" with { type: "json" };

test("the frontend contains usable geography and the complete recording catalog without requests", () => {
  expect(geography.features.length).toBe(177);
  expect(isCatalog(initialCatalog())).toBe(true);
  expect(initialCatalog().sources.length).toBe(20);
  expect(initialCatalog().sources.every(source => source.files.animal.audio && source.events.length > 0)).toBe(true);
});

test("export time and JSON property ordering do not prompt a catalog update", () => {
  const reordered = Object.fromEntries(Object.entries(bundledCatalog).reverse()) as typeof bundledCatalog;
  expect(sameCatalog(bundledCatalog, { ...reordered, created_at_utc: "2099-01-01" })).toBe(true);
  const updated = structuredClone(bundledCatalog);
  updated.sources[0]!.note = "Updated recording details";
  expect(sameCatalog(bundledCatalog, updated)).toBe(false);
  expect(sameCatalog(bundledCatalog, { ...bundledCatalog, config_sha256: "new settings" })).toBe(false);
});

test("an applied server update survives a refresh, while stale and damaged caches use the bundle", () => {
  let cached: string | null = null;
  const storage = { getItem: () => cached, setItem: (_key: string, value: string) => { cached = value; } };
  const updated = structuredClone(bundledCatalog);
  updated.sources[0]!.title = "Updated title";
  saveCatalog(storage, updated);
  expect(initialCatalog(storage).sources[0]!.title).toBe("Updated title");
  cached = JSON.stringify({ bundle: "old frontend", catalog: updated });
  expect(initialCatalog(storage)).toBe(bundledCatalog);
  cached = "broken JSON";
  expect(initialCatalog(storage)).toBe(bundledCatalog);
  expect(initialCatalog({ getItem: () => { throw Error("Storage blocked"); } })).toBe(bundledCatalog);
  for (const data of [null, [], {}, { ...updated, sources: [{ id: "invalid", location: { lon: "bad", lat: 0 } }] }])
    expect(isCatalog(data)).toBe(false);
});
