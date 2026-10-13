import { readdir } from "node:fs/promises";
import { join } from "node:path";
import type { Manifest } from "../src/lib/types.ts";

export const PRECOMPUTED_FILES = ["duos.json", "trios.json"];

/** Include canonical, model-specific and fingerprinted manifests, excluding runner metadata. */
export async function playbackManifests(root: string, id: string): Promise<Map<string, Manifest>> {
  const folder = join(root, "combos", id);
  const names = await readdir(folder).catch(() => [] as string[]);
  const manifests = new Map<string, Manifest>();
  for (const name of names.sort()) {
    if (!/^manifest(?:-[a-z0-9-]+)?\.json$/.test(name)
        && !/^generated-\d+-[a-z0-9-]+-[a-f0-9]+\.json$/.test(name)) continue;
    manifests.set(`combos/${id}/${name}`, await Bun.file(join(folder, name)).json() as Manifest);
  }
  return manifests;
}

export function playbackMedia(manifest: Manifest): string[] {
  return [
    ...Object.values(manifest.stems).flatMap(stem => stem ? [stem.audio, stem.image] : []),
    ...manifest.parts.flatMap(part => [part.files.animal, part.files.guide]),
  ];
}
