import appConfig from "../../../../resources/orchestra-app.json" with { type: "json" };
import type { Manifest, ModelVariant, ModelRegistry } from "./types.ts";

export const bundledModels = appConfig.music as ModelRegistry;

export function isModelRegistry(value: unknown): value is ModelRegistry {
  if (!value || typeof value !== "object") return false;
  const registry = value as ModelRegistry;
  if (!Array.isArray(registry.models) || !registry.models.length) return false;
  const ids = registry.models.map(m => m.id);
  const profiles = registry.caption_profiles;
  if (profiles !== undefined && (!profiles || typeof profiles !== "object" || Array.isArray(profiles)
      || Object.entries(profiles).some(([id, profile]) => !/^[a-z][a-z0-9-]{0,63}$/.test(id) || !["legacy", "instruments"].includes(profile)))) return false;
  return new Set(ids).size === ids.length && ids.includes(registry.default_model) && ids.includes(registry.primary_model)
    && registry.models.every(m => /^[a-z][a-z0-9-]{0,63}$/.test(m.id) && typeof m.label === "string" && typeof m.description === "string"
      && Array.isArray(m.characteristics) && m.characteristics.every(c => typeof c === "string")
      && ["ace-step", "stable-audio-3"].includes(m.engine) && typeof m.checkpoint === "string" && Number.isInteger(m.steps) && m.steps > 0
      && Number.isFinite(m.guidance_scale) && m.guidance_scale >= 0);
}

export function modelLabel(id: string, registry = bundledModels): string {
  return registry.models.find(m => m.id === id)?.label ?? id;
}

/** Canonical numeric JSON, shared with Python's model_fingerprint. */
export function modelSettings(model: object): string {
  const display = new Set(["label","description","recommended","characteristics","available","fingerprint","device","reuse_primary"]);
  return JSON.stringify(Object.fromEntries(Object.entries(model).filter(([key])=>!display.has(key)).sort(([a],[b])=>a.localeCompare(b))));
}

export function modelMatches(manifest: Manifest, model: typeof bundledModels.models[number]): boolean {
  if (!manifest.stems.ace || renderedModel(manifest) !== model.id) return false;
  if (manifest.generation?.model_config_sha256 && model.fingerprint) return manifest.generation.model_config_sha256 === model.fingerprint;
  return manifest.ace?.inference_steps === undefined || manifest.ace.inference_steps === model.steps && manifest.ace.guidance_scale === model.guidance_scale;
}

/** Earlier generated pieces used Base before model selection was available. */
export function renderedModel(manifest: Manifest): ModelVariant {
  return manifest.generation?.model_id ?? manifest.ace?.variant ?? (manifest.ace?.model === "acestep-v15-turbo" ? "turbo" : "base");
}

export function modelSatisfies(saved: ModelVariant, requested: ModelVariant): boolean {
  return saved === requested;
}
