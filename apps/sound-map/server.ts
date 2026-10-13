// Sound map server: serves the app, the catalog and listening files, and runs combinations.
// All measurement, generation and scoring happens in Python (`main.py follow ...`); this file only
// writes a spec, runs the CLI and streams its readable log lines back as NDJSON.

import { mkdir, readdir } from "node:fs/promises";
import { join, resolve } from "node:path";
import index from "./src/index.html";
import { CompositionJobs, type TrackedComposition } from "./src/lib/combine-jobs.ts";
import { compositionFailure, compositionProgress } from "./src/lib/combine-progress.ts";
import { specKey } from "./src/lib/precomputed.ts";
import { contentType, readableLog, safeJoin } from "./src/lib/paths.ts";
import { SILVER_AUDIO_METHODS } from "./src/lib/silver-audio.ts";
import { SILVER_AUDIO_RUNTIME } from "./src/lib/silver-performance.ts";
import type { Catalog, ComboSummary, GenerationRuntime, Manifest, Spec } from "./src/lib/types.ts";
import { bundledModels, isModelRegistry, modelMatches, modelSettings, renderedModel } from "./src/lib/model-variant.ts";

declare const WHALES_BUILD_ROOT: string; // replaced by the production build
const ROOT = resolve(process.env.WHALES_ROOT ?? (typeof WHALES_BUILD_ROOT === "string" ? WHALES_BUILD_ROOT : join(import.meta.dir, "..", "..")));
const APP = join(ROOT, "apps", "sound-map");
const OUTPUT = join(ROOT, "data", "output", "follow");
// Committed bundle (Git LFS, `bun scripts/demo.ts`): read when OUTPUT lacks a file, so a fresh clone plays.
const DEMO = join(APP, "demo", "follow");
// The Sound to silver tab's files: the bench and motion pages from `main.py art silver`, else the committed copy.
const SILVER = [join(ROOT, "data", "output", "silver"), join(APP, "demo", "silver")];
const SILVER_TYPES: Record<string, string> = { ".html": "text/html; charset=utf-8", ".jpg": "image/jpeg", ".svg": "image/svg+xml" };
const SPECS = join(ROOT, "data", "interim", "follow", "combo-specs");
// Preset names live beside the pieces, not in their manifests: re-running a piece rewrites its manifest,
// and a name must never change a piece's id.
const PRESETS = join(OUTPUT, "presets.json");
type Presets = Record<string, { name: string; saved_at_utc: string }>;
// The demo bundle ships the names of its presets, so a fresh clone shows them too; names saved here win.
const readPresets = async (): Promise<Presets> => ({
  ...(await Bun.file(join(DEMO, "presets.json")).json().catch(() => ({}))),
  ...(await Bun.file(PRESETS).json().catch(() => ({}))),
}) as Presets;
// The repository's .env sets ORCHESTRA_PORT and ORCHESTRA_HOST (`uv run start`, `bun run dev`); PORT and HOST still work.
const PORT = Number(process.env.ORCHESTRA_PORT ?? process.env.PORT ?? 3070);
const HOSTNAME = process.env.ORCHESTRA_HOST ?? process.env.HOST ?? "127.0.0.1";
const PYTHON = ["uv", "run", "--group", "audio", "main.py", "follow"];
const MAX_SPEC_BYTES = 64 * 1024;

const compositions = new CompositionJobs(); // one combination at a time: the GPU is shared
let runtimeProbe: { expires: number; result: Promise<GenerationRuntime> } | null = null;

async function generationRuntime(): Promise<Response> {
  if (!runtimeProbe || runtimeProbe.expires < Date.now()) {
    const result = (async (): Promise<GenerationRuntime> => {
      const executablePython = join(ROOT, ".venv", "bin", "python");
      let pythonAvailable = false;
      if (Bun.which("uv") && await Bun.file(executablePython).exists() && await Bun.file(join(ROOT, "main.py")).exists()) {
        try {
          const probe = Bun.spawn([executablePython, "-c", "import numpy, scipy, soundfile, matplotlib, PIL; import experiments.follow_combine"], {
            cwd: ROOT, env: process.env, stdout: "ignore", stderr: "ignore", timeout: 8_000,
          });
          pythonAvailable = await probe.exited === 0;
        } catch { /* Playback works without a Python installation. */ }
      }
      const unavailable: GenerationRuntime = { python_available: pythonAvailable, ace_available: false, cuda_available: null, device: null };
      if (!pythonAvailable) return unavailable;
      try {
        // Checks authenticated HF readiness and the local ACE-Step environment.
        // The token remains server-side; runtime reports only backend status.
        const child = Bun.spawn([executablePython, "-m", "experiments.follow_hf", "--runtime"], {
          cwd: ROOT, env: process.env, stdout: "pipe", stderr: "ignore", timeout: 12_000,
        });
        const output = await new Response(child.stdout).text();
        if (await child.exited !== 0) return unavailable;
        return { python_available: pythonAvailable, ace_available: true, ...JSON.parse(output) } as GenerationRuntime;
      } catch { return unavailable; }
    })();
    runtimeProbe = { expires: Date.now() + 30_000, result };
  }
  return Response.json(await runtimeProbe.result, { headers: { "cache-control": "no-store" } });
}

async function python(args: string[], onLine?: (line: string) => void): Promise<{ code: number; tail: string[] }> {
  const child = Bun.spawn([...PYTHON, ...args], {
    cwd: ROOT,
    env: { ...process.env, PYTHONUNBUFFERED: "1" },
    stdout: "pipe",
    stderr: "pipe",
  });
  const tail: string[] = [];
  const pump = async (stream: ReadableStream<Uint8Array>) => {
    const decoder = new TextDecoder();
    const reader = stream.getReader();
    let buffer = "";
    for (;;) {
      const { value: chunk, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(chunk, { stream: true });
      const lines = buffer.split(/\r?\n|\r/);
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        tail.push(line);
        if (tail.length > 40) tail.shift();
        onLine?.(line);
      }
    }
    buffer += decoder.decode();
    if (buffer) {
      tail.push(buffer);
      if (tail.length > 40) tail.shift();
      onLine?.(buffer);
    }
  };
  await Promise.all([pump(child.stdout), pump(child.stderr)]);
  return { code: await child.exited, tail };
}

async function catalogFile(file: ReturnType<typeof Bun.file>, request?: Request): Promise<Response> {
  await file.exists(); // Load file metadata before constructing its cache validator.
  const etag = `W/"${file.size}-${file.lastModified}"`;
  const headers = { "content-type": "application/json", "cache-control": "no-cache", etag };
  return request?.headers.get("if-none-match") === etag ? new Response(null, { status: 304, headers }) : new Response(file, { headers });
}

async function catalog(request?: Request): Promise<Response> {
  const file = Bun.file(join(OUTPUT, "catalog.json"));
  const demo = Bun.file(join(DEMO, "catalog.json"));
  const prepared = await Bun.file(join(ROOT, "data", "interim", "follow", "prepared.json")).exists();
  if (!(await file.exists()) && !prepared && (await demo.exists())) {
    return catalogFile(demo, request);
  }
  if (!(await file.exists())) {
    const runtime = await (await generationRuntime()).json() as GenerationRuntime;
    if (!runtime.python_available) {
      if (await demo.exists()) return catalogFile(demo, request);
      return Response.json({ error: "The Python engine is unavailable and no playback catalog is installed." }, { status: 503 });
    }
    const { code, tail } = await python(["catalog"]);
    if (code !== 0) {
      if (await demo.exists()) return catalogFile(demo, request);
      return Response.json({ error: "Could not load the catalog. Install the prepared dataset with 'main.py follow dataset download'.", log: tail }, { status: 500 });
    }
  }
  return catalogFile(Bun.file(join(OUTPUT, "catalog.json")), request);
}

async function combos(): Promise<Response> {
  const out: ComboSummary[] = [];
  const seen = new Set<string>();
  const presets = await readPresets();
  for (const folder of [join(OUTPUT, "combos"), join(DEMO, "combos")]) {
    for (const name of await readdir(folder).catch(() => [] as string[])) {
      const file = Bun.file(join(folder, name, "manifest.json"));
      if (seen.has(name) || !(await file.exists())) continue;
      seen.add(name);
      const m = (await file.json()) as Manifest;
      const preset = presets[m.id];
      out.push({ id: m.id, title: preset?.name ?? m.title, created_at_utc: preset?.saved_at_utc ?? m.created_at_utc, duration_s: m.duration_s,
                 sources: m.parts.map((p) => p.source), ace: Boolean(m.stems.ace), model: m.stems.ace ? renderedModel(m) : undefined, preset: Boolean(preset) });
    }
  }
  out.sort((a, b) => b.created_at_utc.localeCompare(a.created_at_utc));
  return Response.json(out);
}

async function musicModels() {
  const config = await Bun.file(join(ROOT, "resources", "orchestra-app.json")).json().catch(() => null);
  if (config && !isModelRegistry(config.music)) throw new Error("The music model configuration is invalid");
  const registry = config?.music ?? bundledModels;
  return { ...registry, models: registry.models.map((m: typeof bundledModels.models[number]) => ({...m,
    fingerprint:new Bun.CryptoHasher("sha256").update(modelSettings(m)).digest("hex"),
  })) };
}

async function models(): Promise<Response> {
  try {
    const registry = await musicModels();
    const runtime = await (await generationRuntime()).json() as GenerationRuntime;
    const stablePython = join(ROOT, "data", "interim", "tools", "Stable-Audio-3", ".venv", "bin", "python");
    let stableDevice: "cuda" | "cpu" | null = null;
    if (await Bun.file(stablePython).exists()) {
      try {
        const probe=Bun.spawn([stablePython,join(ROOT,"experiments","follow_stable.py"),"--runtime"],{cwd:ROOT,stdout:"pipe",stderr:"ignore",timeout:8_000});
        const output=await new Response(probe.stdout).text();
        if (await probe.exited===0) stableDevice=JSON.parse(output).device;
      } catch { /* The UI shows an unavailable model instead of a broken environment. */ }
    }
    return Response.json({ ...registry, models: registry.models.map((m: typeof bundledModels.models[number]) => ({
      ...m, available: runtime.python_available && (m.engine === "stable-audio-3" ? Boolean(stableDevice) : runtime.ace_available),
      device:m.engine === "stable-audio-3" ? stableDevice : runtime.device,
    })) }, { headers: { "cache-control": "no-store" } });
  } catch { return Response.json({ error: "The music model configuration could not load." }, { status: 503 }); }
}

async function combo(id: string, request?: Request): Promise<Response> {
  if (!/^[0-9a-f]{12}$/.test(id)) return Response.json({ error: "Unknown combination" }, { status: 404 });
  const selected = request ? new URL(request.url).searchParams.get("model") : null;
  const registry = await musicModels();
  if (selected && !registry.models.some((m: typeof bundledModels.models[number]) => m.id === selected)) return Response.json({ error: "Unknown music model" }, { status: 400 });
  for (const root of [OUTPUT, DEMO]) {
    const names = selected ? [`manifest-${selected}.json`, "manifest.json"] : ["manifest.json"];
    for (const name of names) {
      const file = Bun.file(join(root, "combos", id, name));
      if (!(await file.exists())) continue;
      const manifest = (await file.json()) as Manifest;
      if (selected && !modelMatches(manifest, registry.models.find((m: typeof bundledModels.models[number]) => m.id === selected)!)) continue;
      const preset = (await readPresets())[id];
      return Response.json(preset ? { ...manifest, title: preset.name, preset: true } : manifest);
    }
  }
  return Response.json({ error: "Unknown combination" }, { status: 404 });
}

/** Name a piece as a preset (an empty name removes it). Only the name is stored; the piece stays as rendered. */
async function savePreset(request: Request, id: string): Promise<Response> {
  if (!/^[0-9a-f]{12}$/.test(id)) return Response.json({ error: "Unknown combination" }, { status: 404 });
  const known = await Promise.all([OUTPUT, DEMO].map((root) => Bun.file(join(root, "combos", id, "manifest.json")).exists()));
  if (!known.some(Boolean)) return Response.json({ error: "Unknown combination" }, { status: 404 });
  const body = (await request.json().catch(() => null)) as { name?: unknown } | null;
  const name = typeof body?.name === "string" ? body.name.trim().slice(0, 120) : "";
  const presets = await readPresets();
  if (name) presets[id] = { name, saved_at_utc: new Date().toISOString() };
  else delete presets[id];
  await mkdir(OUTPUT, { recursive: true });
  await Bun.write(PRESETS, JSON.stringify(presets, null, 1));
  return Response.json({ id, name: name || null });
}

async function combine(request: Request): Promise<Response> {
  const body = await request.text();
  if (body.length > MAX_SPEC_BYTES) return Response.json({ error: "The spec is too large." }, { status: 413 });
  let spec: Spec;
  try {
    spec = JSON.parse(body);
  } catch {
    return Response.json({ error: "The spec is not valid JSON." }, { status: 400 });
  }
  const url = new URL(request.url);
  const requested = url.searchParams.get("model");
  const registry = await musicModels();
  const variant = requested && !["0", "1"].includes(requested) ? requested : registry.default_model;
  const selected = registry.models.find((m: typeof bundledModels.models[number]) => m.id === variant);
  if (!selected) {
    return Response.json({ error: "Choose one of the configured music models." }, { status: 400 });
  }
  const model = requested !== "0" && Boolean(spec.ace);
  if (model && variant !== "base" && spec.ace?.task === "lego") {
    return Response.json({ error: "Choose Base for adding a track. This model supports cover composition." }, { status: 400 });
  }
  let key: string;
  try {
    // The same defaults and layer ordering used by saved-piece lookup in the UI.
    spec = { ...spec, ace: model ? spec.ace : null };
    const sourceCatalog = await (await catalog()).json() as Catalog;
    key = sourceCatalog.config_sha256 + specKey(spec, sourceCatalog);
  } catch {
    return Response.json({ error: "The combination settings could not be read. Choose recordings and try again." }, { status: 400 });
  }
  const jobKey = key + (model ? `:${variant}:${selected.fingerprint}` : ":recordings");
  const pending = compositions.pending(jobKey);
  if (pending) return compositionStream(pending);
  const runtime = await (await generationRuntime()).json() as GenerationRuntime;
  if (!runtime.python_available) return Response.json({ error: "The Python composition engine is unavailable. Recordings and saved pieces still play." }, { status: 503 });
  if (model && selected.engine === "stable-audio-3" && !(await Bun.file(join(ROOT, "data", "interim", "tools", "Stable-Audio-3", ".venv", "bin", "python")).exists())) {
    return Response.json({ error: "Stable Audio is not installed on this server. Choose another model." }, { status: 503 });
  }

  // Reserve synchronously after the runtime probe. Different jobs queue; matching jobs join.
  const job = compositions.start(jobKey, spec, async (send, id) => {
    await mkdir(SPECS, { recursive: true });
    const specPath = join(SPECS, `${id}.json`);
    await Bun.write(specPath, JSON.stringify(spec, null, 2));
    const args = ["combine", specPath, ...(model ? ["--model-variant", variant] : ["--no-model"])];
    let failure: string | null = null;
    const { code, tail } = await python(args, (line) => {
      if (line.trim().startsWith("ERROR: ")) failure = line.trim().slice(7);
      const progress = compositionProgress(line);
      if (progress) send(progress);
      else if (readableLog(line) && !line.trim().startsWith("ERROR: ")) send({ type: "log", text: line.trim() });
    });
    const manifestPath = tail.map((line) => line.trim()).filter((line) => line.endsWith("manifest.json")).at(-1);
    if (code === 0 && manifestPath) {
      send({ type: "done", manifest: (await Bun.file(manifestPath).json()) as Manifest });
    } else {
      send({ type: "error", message: failure || compositionFailure(tail, code) });
    }
  }, variant);
  return compositionStream(job);
}

function compositionStream(job: TrackedComposition): Response {
  return new Response(job.stream(), { headers: { "content-type": "application/x-ndjson", "cache-control": "no-store" } });
}

function reconnectComposition(id: string): Response {
  const job = compositions.get(id);
  return job ? compositionStream(job) : Response.json({ error: "This composition can no longer be tracked. Please compose the piece again." }, { status: 404 });
}

async function files(request: Request): Promise<Response> {
  const requested = new URL(request.url).pathname.slice("/files/".length);
  for (const root of [OUTPUT, DEMO]) {
    const path = safeJoin(root, requested);
    if (!path) return new Response("Not found", { status: 404 });
    const file = Bun.file(path);
    if (await file.exists()) {
      return new Response(file, { headers: { "content-type": contentType(path), "cache-control": "no-cache" } });
    }
  }
  return new Response("Not found", { status: 404 });
}

async function silverFiles(request: Request): Promise<Response> {
  const requested = new URL(request.url).pathname.slice("/silver/files/".length);
  for (const root of SILVER) {
    const path = safeJoin(root, requested);
    if (!path) return new Response("Not found", { status: 404 });
    const file = Bun.file(path);
    if (await file.exists()) {
      const type = SILVER_TYPES[path.slice(path.lastIndexOf(".")).toLowerCase()] ?? contentType(path);
      return new Response(file, { headers: { "content-type": type, "cache-control": "no-cache" } });
    }
  }
  return new Response("Not found", { status: 404 });
}

// Reuse the generated bench's rendering and sound analysis inside the project page.
// A shadow root keeps its controls separate from the orchestra's own IDs and styles.
async function silverViewerModule(): Promise<Response> {
  for (const root of SILVER) {
    const file = Bun.file(join(root, "index.html"));
    if (!(await file.exists())) continue;
    const html = await file.text();
    const source = html.match(/<script type="module">([\s\S]*?)<\/script>/)?.[1];
    if (!source) return new Response("The ring viewer module is missing", { status: 500 });
    const module = source
      .replace('from "three"', 'from "https://cdn.jsdelivr.net/npm/three@0.170.0/build/three.module.js"')
      .replace(/from "three\/addons\/([^\"]+)"/g, 'from "https://cdn.jsdelivr.net/npm/three@0.170.0/examples/jsm/$1"')
      .replace('const DATA = JSON.parse(document.getElementById("silver-data").textContent);', 'export function mountViewer(root, DATA) {')
      .replace('document.getElementById(id)', 'root.getElementById(id)')
      .replace('getComputedStyle(document.documentElement)', 'getComputedStyle(root.host)')
      .replace('const w = stage.clientWidth, h = stage.clientHeight;', 'const w = stage.clientWidth, h = stage.clientHeight; if (!w || !h) return;')
      .replace(' · ${piece.triangle_count.toLocaleString()} triangles shown', '')
      .replace('mode: "silver", ghost: true', 'mode: "silver", ghost: false')
      .replace('const INTEGRATED_VIEWER = false;', 'const INTEGRATED_VIEWER = true;')
      .replace('ghost.visible = true; scene.add(ghost);', 'ghost.visible = false; scene.add(ghost);')
      .replaceAll('"Play and bend"', '"▶ Play sound"')
      .replace('textContent = "Stop"', 'textContent = "■ Stop sound"')
      .replace('Drag to turn the ring. Play bends it as the sound plays.', 'Drag to turn the ring.');
    return new Response(`${module}
${SILVER_AUDIO_RUNTIME}
let previewOrigin = performance.now(), lastPreview = 0, previousPlaying = false;
let previewSound = state.sound, previewPiece = state.piece;
function animateViewer() {
  const now = performance.now(), sound = state.live || state.sound;
  if (state.playing !== previousPlaying || sound !== previewSound || state.piece !== previewPiece) {
    previewOrigin = now; previousPlaying = state.playing; previewSound = sound; previewPiece = state.piece;
  }
  if (!state.playing && !state.held && !(state.piece.id === "inline" && state.spineNeutral) && now - lastPreview >= (rotating ? 150 : 50)) {
    // A silent loop makes the sound-to-form interaction visible before the first click.
    const elapsed = (now - previewOrigin) / 1000 * Number($("speed").value);
    state.t = Math.min(elapsed % (sound.duration_s + 0.35), sound.duration_s);
    update(); lastPreview = now;
  }
  const note=state.piece.id === "inline" && state.spineNeutral ? "Neutral spine" : state.playing ? (Number($("speed").value) === 1 ? "Playing" : "Playing at ¼ speed") : state.held ? "Generated shape · drag to inspect" : "Silent preview";
  if ($("preview-note").textContent !== note) $("preview-note").textContent=note;
  for (const button of root.querySelectorAll(".spine-view button")) {
    const pressed=String((button.dataset.neutral === "true") === state.spineNeutral);
    if (button.getAttribute("aria-pressed") !== pressed) button.setAttribute("aria-pressed",pressed);
  }
  const playing=String(state.playing);
  if ($("play").getAttribute("aria-pressed") !== playing) $("play").setAttribute("aria-pressed",playing);
  arcLine.visible = state.playing;
  const moved=controls.update();
  if (moved || needsRender) { renderer.render(scene, camera); needsRender=false; }
}
return { ${SILVER_AUDIO_METHODS} snapshot() {
  // Copy the displayed metal synchronously so playback cannot change this export.
  const meshes = [mesh, ...(rootsHeads.visible ? [rootsHeads] : [])].map(object => ({
    positions: new Float32Array(object.geometry.getAttribute("position").array),
    indices: new Uint32Array(object.geometry.getIndex().array),
  }));
  const sound = state.live || state.sound;
  const shape = state.piece.id === "inline" && state.spineNeutral ? "neutral" : sound.id;
  const moment = Number.isFinite(state.t) ? state.t.toFixed(2) + "s" : "complete";
  return { meshes, name: state.piece.id + "-" + shape + "-" + moment };
}, setSpineNeutral(neutral) { stop(); state.held = false; state.spineNeutral = neutral; previewOrigin = performance.now(); state.t = 0; update(); }, setActive(active) {
  if (!active) stop();
  renderer.setAnimationLoop(active ? animateViewer : null);
  if (active) resize();
} };
}
`, {
      headers: { "content-type": "text/javascript; charset=utf-8", "cache-control": "no-cache" },
    });
  }
  return new Response("The ring viewer is missing", { status: 404 });
}

const server = Bun.serve({
  port: PORT,
  hostname: HOSTNAME,
  development: process.env.NODE_ENV !== "production",
  idleTimeout: 255, // Heartbeats keep slow ACE-Step CPU runs connected.
  routes: {
    "/": index,
    "/silver": () => Response.redirect("/?view=silver", 302), // the page is now a tab; old #idea links keep their hash
    "/silver/viewer.js": { GET: silverViewerModule },
    "/silver/files/*": { GET: silverFiles },
    "/api/catalog": { GET: catalog },
    "/api/runtime": { GET: generationRuntime },
    "/api/combos": { GET: combos },
    "/api/models": { GET: models },
    "/api/combos/:id": { GET: (request) => combo(request.params.id, request) },
    "/api/combos/:id/preset": { POST: (request) => savePreset(request, request.params.id) },
    "/api/combine": { POST: combine, GET: () => Response.json({ job: compositions.active?.info ?? null }, { headers: { "cache-control": "no-store" } }) },
    "/api/combine/:id": { GET: (request) => reconnectComposition(request.params.id) },
    "/files/*": { GET: files },
  },
  fetch: () => new Response("Not found", { status: 404 }),
});

console.log(`Sound map: ${server.url} (data: ${OUTPUT})`);
