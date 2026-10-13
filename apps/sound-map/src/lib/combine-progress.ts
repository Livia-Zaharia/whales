import type { CompositionProgress } from "./types.ts";

const PREFIX = "COMPOSE_PROGRESS ";

/** Structured engine progress is separate from readable diagnostics. */
export function compositionProgress(line: string): CompositionProgress | null {
  if (!line.startsWith(PREFIX)) return null;
  try {
    const value = JSON.parse(line.slice(PREFIX.length)) as { percent?: unknown; stage?: unknown };
    if (typeof value.stage !== "string" || typeof value.percent !== "number" || !Number.isFinite(value.percent)) return null;
    return { type: "progress", percent: Math.max(0, Math.min(99, value.percent)), stage: value.stage };
  } catch { return null; }
}

/** Prefer the runner's cause over a parent subprocess traceback. */
export function compositionFailure(tail: string[], code: number): string {
  const explicit = tail.filter((line) => line.trim().startsWith("ERROR: ")).at(-1);
  return explicit?.trim().slice(7) || tail.filter((line) => /\b\w*Error:/.test(line) && !line.includes("CalledProcessError:")).at(-1)?.trim()
    || `The composition engine exited with code ${code}. Please try again.`;
}
