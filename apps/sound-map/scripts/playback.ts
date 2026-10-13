import { readdir } from "node:fs/promises";
import { join } from "node:path";
import type { Manifest } from "../src/lib/types.ts";
import { precomputedFiles } from "../src/lib/precomputed.ts";
import { bundledModels } from "../src/lib/model-variant.ts";

export const PRECOMPUTED_FILES = precomputedFiles(bundledModels.models.map(model => model.id));

/** Include canonical, model-specific and fingerprinted manifests, excluding runner metadata. */
export async function playbackManifests(root: string, id: string): Promise<Map<string, Manifest>> {
  const folder = join(root, "combos", id);
  const names = await readdir(folder).catch(() => [] as string[]);
  const canonical = await Bun.file(join(folder, "manifest.json")).json().catch(() => null) as Manifest | null;
  const manifests = new Map<string, Manifest>();
  for (const name of names.sort()) {
    if (name === "manifest-constructed.json" && canonical?.stems.ace) continue;
    if (!/^manifest(?:-[a-z0-9-]+)?\.json$/.test(name)
        && !/^generated-\d+-[a-z0-9-]+-[a-f0-9]+\.json$/.test(name)) continue;
    manifests.set(`combos/${id}/${name}`, await Bun.file(join(folder, name)).json() as Manifest);
  }
  return manifests;
}

export function playbackMedia(manifest: Manifest): string[] {
  return [
    ...Object.values(manifest.stems).flatMap(stem => stem ? [
      ...(stem.audio ? [stem.audio] : []), stem.image, ...(stem.components ?? []).map(component => component.audio),
    ] : []),
    ...manifest.parts.flatMap(part => [part.files.animal, part.files.guide]),
  ];
}

/** The player mixes shared sources; shipping another mixed recording/guide/response is unnecessary. */
export function compactPlayback(manifest: Manifest): Manifest {
  const compact = structuredClone(manifest);
  if (compact.stems.ace) {
    for (const stem of Object.values(compact.stems)) if (stem?.components?.length) delete stem.audio;
  }
  return compact;
}
