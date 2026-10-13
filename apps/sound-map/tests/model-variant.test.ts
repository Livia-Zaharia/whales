import { expect, test } from "bun:test";
import { bundledModels, isModelRegistry, modelLabel, modelSatisfies, renderedModel } from "../src/lib/model-variant.ts";
import type { Manifest } from "../src/lib/types.ts";

test("each selected model opens its own result, and earlier pieces are recognized as Base", () => {
  expect(modelSatisfies("base", "base")).toBe(true);
  expect(modelSatisfies("base", "turbo")).toBe(false);
  expect(modelSatisfies("turbo", "turbo")).toBe(true);
  expect(modelSatisfies("turbo", "base")).toBe(false);
  expect(renderedModel({ ace: { variant: "turbo" } } as Manifest)).toBe("turbo");
  expect(renderedModel({ ace: { model: "acestep-v15-turbo" } } as Manifest)).toBe("turbo");
  expect(renderedModel({ ace: { caption: "Older generated piece" } } as Manifest)).toBe("base");
  expect(renderedModel({ generation: {model_id:"stable-audio-3-small"}, ace:null } as Manifest)).toBe("stable-audio-3-small");
  expect(modelSatisfies("stable-audio-3-small", "base")).toBe(false);
});

test("app config supplies defaults, recommendations and editable characteristics", () => {
  expect(isModelRegistry(bundledModels)).toBe(true);
  expect(bundledModels.default_model).toBe("base");
  expect(bundledModels.models.filter(m => m.recommended).map(m => m.id)).toEqual(["base","stable-audio-3-small"]);
  expect(modelLabel("stable-audio-3-small")).toBe("Stable Audio 3 Small Music");
  expect(isModelRegistry({...bundledModels,default_model:"missing"})).toBe(false);
  expect(isModelRegistry({...bundledModels,models:[...bundledModels.models,bundledModels.models[0]]})).toBe(false);
  expect(isModelRegistry({...bundledModels,models:[{...bundledModels.models[0],id:"../base"}]})).toBe(false);
});
