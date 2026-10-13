import { expect, test } from "bun:test";
import { compactPlayback, playbackMedia } from "../scripts/playback.ts";
import { playbackGains, playbackTracks } from "../src/lib/playback-tracks.ts";
import { SavedVersions } from "../src/lib/saved-versions.ts";
import type { Manifest } from "../src/lib/types.ts";

const manifest = {
  id: "abcdef123456", parts: [{ files: { animal: "source/animal.mp3", guide: "shared/guide.mp3" }, playback_gain: { animal: .2, guide: .4 } }],
  stems: {
    animal: { audio: "duplicate-animal.mp3", image: "animal.webp", components: [{ audio: "source/animal.mp3", gain: .2 }] },
    guide: { audio: "duplicate-guide.mp3", image: "guide.webp", components: [{ audio: "shared/guide.mp3", gain: .4 }] },
    response: { audio: "duplicate-response.mp3", image: "response.webp", components: [{ audio: "shared/response.mp3", gain: .3 }] },
    ace: { audio: "generated.mp3", image: "generated.webp" },
  },
} as unknown as Manifest;

test("compact generated pieces reuse original recordings and shared guides/responses", () => {
  const compact = compactPlayback(manifest);
  const media = playbackMedia(compact);
  expect(media).not.toContain("duplicate-animal.mp3");
  expect(media).not.toContain("duplicate-guide.mp3");
  expect(media).not.toContain("duplicate-response.mp3");
  expect(media).toContain("source/animal.mp3");
  expect(media).toContain("shared/response.mp3");
  expect(manifest.stems.animal?.audio).toBe("duplicate-animal.mp3");
  const on = { animal: true, guide: false, response: true, ace: true };
  const gains = playbackGains(compact, on, [true]);
  const tracks = playbackTracks(compact, gains);
  expect(tracks.find(track => track.key === "animal-0")?.gain).toBe(.2);
  expect(tracks.find(track => track.key === "guide-0")?.gain).toBe(0);
  expect(tracks.find(track => track.key === "response-0")?.gain).toBe(.3);
  expect(tracks.find(track => track.key === "ace")?.gain).toBe(1);
  expect(playbackGains(compact, on, [false])["animal-0"]).toBe(0);
});

test("legacy pieces continue using their baked track gains and mixed response", () => {
  const legacy = structuredClone(manifest);
  delete legacy.parts[0]!.playback_gain;
  for (const stem of Object.values(legacy.stems)) delete stem!.components;
  const gains = playbackGains(legacy, { animal: true, guide: true, response: true, ace: false }, [true]);
  const tracks = playbackTracks(legacy, gains);
  expect(gains["animal-0"]).toBe(1);
  expect(tracks.find(track => track.key === "response")?.url).toBe("/files/duplicate-response.mp3");
  expect(compactPlayback(legacy).stems.animal?.audio).toBe("duplicate-animal.mp3");
});

test("model caches retain multiple versions of one shared spec and reject changed settings", () => {
  const saved = new SavedVersions();
  saved.remember("same-spec", "base", "base-piece", "base-settings");
  saved.remember("same-spec", "stable-audio-3-small", "stable-piece", "stable-settings");
  expect(saved.find("same-spec", "base", "base-settings")).toBe("base-piece");
  expect(saved.find("same-spec", "stable-audio-3-small", "stable-settings")).toBe("stable-piece");
  expect(saved.find("same-spec", "base", "changed-settings")).toBeUndefined();
  expect(saved.find("same-spec", "turbo")).toBeUndefined();
});
