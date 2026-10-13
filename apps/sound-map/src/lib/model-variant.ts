import type { Manifest, ModelVariant } from "./types.ts";

/** Earlier generated pieces used Base before model selection was available. */
export function renderedModel(manifest: Manifest): ModelVariant {
  return manifest.ace?.variant ?? (manifest.ace?.model === "acestep-v15-turbo" ? "turbo" : "base");
}

export function modelSatisfies(saved: ModelVariant, requested: ModelVariant): boolean {
  return saved === "base" || requested === "turbo";
}
