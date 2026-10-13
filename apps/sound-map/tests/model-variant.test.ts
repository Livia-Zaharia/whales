import { expect, test } from "bun:test";
import { modelSatisfies, renderedModel } from "../src/lib/model-variant.ts";
import type { Manifest } from "../src/lib/types.ts";

test("Base fulfills either request, while Turbo remains eligible for a Base upgrade", () => {
  expect(modelSatisfies("base", "base")).toBe(true);
  expect(modelSatisfies("base", "turbo")).toBe(true);
  expect(modelSatisfies("turbo", "turbo")).toBe(true);
  expect(modelSatisfies("turbo", "base")).toBe(false);
  expect(renderedModel({ ace: { variant: "turbo" } } as Manifest)).toBe("turbo");
  expect(renderedModel({ ace: { model: "acestep-v15-turbo" } } as Manifest)).toBe("turbo");
  expect(renderedModel({ ace: { caption: "Older generated piece" } } as Manifest)).toBe("base");
});
