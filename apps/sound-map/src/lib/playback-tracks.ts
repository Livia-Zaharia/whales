import { fileUrl } from "./api.ts";
import type { Manifest, StemName } from "./types.ts";
import type { Track } from "../player.ts";

export function playbackGains(manifest: Manifest, on: Record<StemName, boolean>, parts: boolean[]): Record<string, number> {
  const gains: Record<string, number> = {};
  manifest.parts.forEach((part, i) => {
    gains[`animal-${i}`] = on.animal && parts[i] ? part.playback_gain?.animal ?? 1 : 0;
    gains[`guide-${i}`] = on.guide && parts[i] ? part.playback_gain?.guide ?? 1 : 0;
  });
  gains.response = on.response ? 1 : 0;
  gains.ace = on.ace ? 1 : 0;
  manifest.stems.response?.components?.forEach((component, i) => {
    gains[`response-${i}`] = on.response ? component.gain : 0;
  });
  return gains;
}

export function playbackTracks(manifest: Manifest, gains: Record<string, number>): Track[] {
  const tracks: Track[] = manifest.parts.flatMap((part, i) => [
    { key: `animal-${i}`, url: fileUrl(part.files.animal), gain: gains[`animal-${i}`] ?? 0 },
    { key: `guide-${i}`, url: fileUrl(part.files.guide), gain: gains[`guide-${i}`] ?? 0 },
  ]);
  for (const key of ["response", "ace"] as const) {
    const stem = manifest.stems[key];
    if (stem?.components?.length) {
      stem.components.forEach((component, i) => tracks.push({ key: `${key}-${i}`, url: fileUrl(component.audio), gain: gains[`${key}-${i}`] ?? 0 }));
    } else if (stem?.audio) tracks.push({ key, url: fileUrl(stem.audio), gain: gains[key] ?? 0 });
  }
  return tracks;
}
