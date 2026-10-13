import type { ModelVariant } from "./types.ts";

/** Preserve separate model results even when they share one resolved ensemble spec. */
export class SavedVersions {
  private readonly versions = new Map<string, Map<ModelVariant | null, { id: string; fingerprint?: string }>>();

  remember(key: string, model: ModelVariant | null, id: string, fingerprint?: string) {
    const models = this.versions.get(key) ?? new Map();
    models.set(model, { id, fingerprint });
    this.versions.set(key, models);
  }

  find(key: string, model: ModelVariant | null, fingerprint?: string): string | undefined {
    const saved = this.versions.get(key)?.get(model);
    if (fingerprint && saved?.fingerprint && fingerprint !== saved.fingerprint) return undefined;
    return saved?.id;
  }

  anyId(key: string): string | undefined {
    return this.versions.get(key)?.values().next().value?.id;
  }
}
