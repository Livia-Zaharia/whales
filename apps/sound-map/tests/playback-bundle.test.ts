import { describe, expect, test } from "bun:test";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { playbackManifests, playbackMedia, PRECOMPUTED_FILES } from "../scripts/playback.ts";
import type { Manifest } from "../src/lib/types.ts";

describe("playback bundle", () => {
  test("exports legacy and model-specific audio without treating runner files as manifests", async () => {
    const root = await mkdtemp(join(tmpdir(), "whales-playback-"));
    const id = "123456abcdef";
    const folder = `combos/${id}`;
    const old = { id, parts: [{ files: { animal: `${folder}/part-1.mp3`, guide: `${folder}/guide-1.mp3` } }],
      stems: { ace: { audio: `${folder}/ace.mp3`, image: `${folder}/ace.webp` } } } as Manifest;
    const base = { ...old, stems: { ace: { audio: `${folder}/generated-7-base-aabbcc1122.mp3`, image: `${folder}/generated-7-base-aabbcc1122.webp` } } } as Manifest;
    const stable = { ...old, stems: { ace: { audio: `${folder}/generated-7-stable-audio-3-445566aabb.mp3`, image: `${folder}/generated-7-stable-audio-3-445566aabb.webp` } } } as Manifest;
    try {
      for (const [name, data] of Object.entries({ "manifest.json": old, "manifest-base.json": base,
        "manifest-stable-audio-3.json": stable, "generated-7-base-aabbcc1122.json": base,
        "generated-7-base-aabbcc1122-results.json": { jobs: {} }, "generated-7-base-aabbcc1122-jobs.json": { jobs: [] } })) {
        await Bun.write(join(root, folder, name), JSON.stringify(data));
      }
      const manifests = await playbackManifests(root, id);
      expect(manifests.size).toBe(4);
      const media = new Set([...manifests.values()].flatMap(playbackMedia));
      expect(media.has(`${folder}/ace.mp3`)).toBe(true);
      expect(media.has(base.stems.ace!.audio!)).toBe(true);
      expect(media.has(stable.stems.ace!.audio!)).toBe(true);
      expect(media.has(`${folder}/part-1.mp3`)).toBe(true);
      expect(PRECOMPUTED_FILES).toContain("duos.json");
      expect(PRECOMPUTED_FILES).toContain("trios-stable-audio-3-small.json");
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });
});
