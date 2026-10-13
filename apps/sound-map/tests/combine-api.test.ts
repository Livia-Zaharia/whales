import { expect, test } from "bun:test";
import { chmod, mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { readComposition } from "../src/lib/combine-reader.ts";
import { bundledModels } from "../src/lib/model-variant.ts";
import type { Catalog, CombineMessage, CompositionJob, Spec } from "../src/lib/types.ts";

test("HTTP requests share matching jobs, queue different inputs, reconnect, and return the correct outcomes", async () => {
  const root = await mkdtemp(join(tmpdir(), "whales-combine-api-"));
  let server: ReturnType<typeof Bun.spawn> | undefined;
  try {
    const output = join(root, "data", "output", "follow");
    const bin = join(root, "bin");
    await Promise.all([mkdir(output, { recursive: true }), mkdir(bin), mkdir(join(root, ".venv", "bin"), { recursive: true })]);
    const catalog = await Bun.file(new URL("../demo/follow/catalog.json", import.meta.url)).json() as Catalog;
    await Bun.write(join(output, "catalog.json"), JSON.stringify(catalog));
    await Bun.write(join(root, "main.py"), "# Test engine; the uv shim handles combinations.");
    const python = join(root, ".venv", "bin", "python");
    await Bun.write(python, "#!/bin/sh\nexit 0\n");
    await chmod(python, 0o755);
    const stablePython = join(root,"data","interim","tools","Stable-Audio-3",".venv","bin","python");
    await Bun.write(stablePython,"# installed model environment");
    const uv = join(bin, "uv");
    await Bun.write(uv, `#!${process.execPath}
import { appendFileSync, writeFileSync, unlinkSync } from "node:fs";
const root = process.env.WHALES_ROOT;
const path = process.argv.find(arg => arg.endsWith(".json"));
const spec = await Bun.file(path).json();
const lock = root + "/engine.lock";
writeFileSync(lock, "running", { flag: "wx" });
const variantIndex = process.argv.indexOf("--model-variant");
const variant = variantIndex < 0 ? "recordings" : process.argv[variantIndex + 1];
appendFileSync(root + "/runs.jsonl", JSON.stringify({...spec, model_variant: variant}) + "\\n");
console.log('COMPOSE_PROGRESS {"percent":55,"stage":"Generating music"}');
if (spec.title === "fail") {
  process.stderr.write("ERROR: ACE-Step: Not enough memory\\nsubprocess.CalledProcessError: engine failed");
  unlinkSync(lock);
  process.exit(1);
}
while (!(await Bun.file(root + "/allow-" + spec.parts[0].trim_s[1]).exists())) await Bun.sleep(20);
const folder = root + "/data/output/follow/combos/" + spec.parts[0].trim_s[1];
const manifest = folder + "/manifest.json";
await Bun.write(manifest, JSON.stringify({ id: spec.title, title: spec.title, spec, ace: spec.ace ? {variant} : null }));
unlinkSync(lock);
console.log(manifest);
`);
    await chmod(uv, 0o755);
    const child = Bun.spawn([process.execPath, new URL("../server.ts", import.meta.url).pathname], {
      env: { ...process.env, WHALES_ROOT: root, ORCHESTRA_HOST: "127.0.0.1", ORCHESTRA_PORT: "0", PATH: `${bin}:${process.env.PATH}` },
      stdout: "pipe", stderr: "pipe",
    });
    server = child;
    const stdout = child.stdout.getReader();
    const ready = new TextDecoder().decode((await stdout.read()).value);
    const url = ready.match(/http:\/\/[^\s]+/)?.[0];
    expect(url).toBeDefined();
    stdout.releaseLock();
    const catalogResponse = await fetch(`${url}api/catalog`);
    const etag = catalogResponse.headers.get("etag");
    expect(etag).toBeTruthy();
    expect((await catalogResponse.json()).sources).toHaveLength(catalog.sources.length);
    const unchanged = await fetch(`${url}api/catalog`, { headers: { "if-none-match": etag! } });
    expect(unchanged.status).toBe(304);
    expect(await unchanged.text()).toBe("");
    const source = catalog.sources[0]!;
    const choices = await (await fetch(`${url}api/models`)).json();
    expect(choices.default_model).toBe("base");
    expect(choices.models.filter((m: {recommended: boolean}) => m.recommended).map((m: {id:string}) => m.id)).toEqual(["base","stable-audio-3-small"]);
    const spec = (end: number, title: string): Spec => ({ title, arrangement: "layer", gap_s: 1, ace: null,
      parts: [{ source: source.id, trim_s: [0, end], offset_s: 0, gain_db: 0, shift_octaves: null }] });
    const post = (body: Spec) => fetch(`${url}api/combine?model=0`, { method: "POST", body: JSON.stringify(body) });
    const readerFor = async (response: Response) => {
      expect(response.status).toBe(200);
      const reader = response.body!.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";
      const next = async (): Promise<CombineMessage> => {
        for (;;) {
          const split = buffer.indexOf("\n");
          if (split >= 0) {
            const line = buffer.slice(0, split);
            buffer = buffer.slice(split + 1);
            if (line.trim()) return JSON.parse(line);
          } else {
            const { value, done } = await reader.read();
            if (done) throw new Error("Unexpected EOF");
            buffer += value;
          }
        }
      };
      const first = await next();
      expect(first.type).toBe("job");
      return { reader, next, job: (first as { type: "job"; job: CompositionJob }).job };
    };
    const first = await readerFor(await post(spec(8, "first")));
    const [duplicate, second] = await Promise.all([readerFor(await post(spec(8, "renamed"))), readerFor(await post(spec(9, "second")))]);
    expect(duplicate.job.id).toBe(first.job.id);
    expect(second.job.id).not.toBe(first.job.id);
    expect(second.job.state).toBe("queued");
    expect(second.job.queue_position).toBe(1);
    await duplicate.reader.cancel();
    while ((await first.next()).type !== "progress" || !(await Bun.file(join(root, "runs.jsonl")).exists())) { /* Wait for the engine. */ }
    expect((await Bun.file(join(root, "runs.jsonl")).text()).trim().split("\n")).toHaveLength(1);
    await Bun.write(join(root, "allow-8"), "go");
    let terminal: CombineMessage;
    do { terminal = await first.next(); } while (terminal.type !== "done" && terminal.type !== "error");
    expect(terminal).toMatchObject({ type: "done", manifest: { id: "first" } });
    await first.reader.cancel();
    // A finished stream remains replayable, including the manifest for playback.
    const replayed: CombineMessage[] = [];
    await readComposition(await fetch(`${url}api/combine/${first.job.id}`), message => replayed.push(message));
    expect(replayed.at(-1)).toMatchObject({ type: "done", manifest: { id: "first" } });
    while ((await second.next()).type !== "progress" || (await Bun.file(join(root, "runs.jsonl")).text()).trim().split("\n").length < 2) { /* Wait for queue advancement. */ }
    await Bun.write(join(root, "allow-9"), "go");
    do { terminal = await second.next(); } while (terminal.type !== "done" && terminal.type !== "error");
    expect(terminal).toMatchObject({ type: "done", manifest: { id: "second" } });
    await second.reader.cancel();
    const failed: CombineMessage[] = [];
    await readComposition(await post(spec(10, "fail")), message => failed.push(message));
    expect(failed.at(-1)).toEqual({ type: "error", message: "ACE-Step: Not enough memory" });
    const modelSpec: Spec = { ...spec(11, "models"), ace: { task: "cover", audio_cover_strength: .6 } };
    const withModel = (model: string) => fetch(`${url}api/combine?model=${model}`, { method: "POST", body: JSON.stringify(modelSpec) });
    const turbo = await readerFor(await withModel("turbo"));
    const base = await readerFor(await withModel("base"));
    const stable = await readerFor(await withModel("stable-audio-3-small"));
    const preferred = await readerFor(await withModel("turbo"));
    expect(turbo.job.model).toBe("turbo");
    expect(base.job.model).toBe("base");
    expect(base.job.id).not.toBe(turbo.job.id);
    expect(base.job.state).toBe("queued");
    expect(stable.job.model).toBe("stable-audio-3-small");
    expect(stable.job.queue_position).toBe(2);
    expect(preferred.job.id).toBe(turbo.job.id);
    await preferred.reader.cancel();
    await Bun.write(join(root, "allow-11"), "go");
    do { terminal = await turbo.next(); } while (terminal.type !== "done" && terminal.type !== "error");
    expect(terminal).toMatchObject({ type: "done", manifest: { ace: { variant: "turbo" } } });
    do { terminal = await base.next(); } while (terminal.type !== "done" && terminal.type !== "error");
    expect(terminal).toMatchObject({ type: "done", manifest: { ace: { variant: "base" } } });
    do { terminal = await stable.next(); } while (terminal.type !== "done" && terminal.type !== "error");
    expect(terminal).toMatchObject({ type:"done",manifest:{ace:{variant:"stable-audio-3-small"}}});
    await stable.reader.cancel();
    await turbo.reader.cancel(); await base.reader.cancel();
    expect((await withModel("unknown")).status).toBe(400);
    const pieceId="0123456789ab";
    const fixture=(model:string) => ({id:pieceId,title:model,parts:[],stems:{ace:{audio:model+".mp3"}},ace:{variant:model}});
    await Bun.write(join(output,"combos",pieceId,"manifest.json"),JSON.stringify(fixture("base")));
    await Bun.write(join(output,"combos",pieceId,"manifest-stable-audio-3-small.json"),JSON.stringify(fixture("stable-audio-3-small")));
    expect((await (await fetch(`${url}api/combos/${pieceId}`)).json()).title).toBe("base");
    expect((await (await fetch(`${url}api/combos/${pieceId}?model=stable-audio-3-small`)).json()).title).toBe("stable-audio-3-small");
    expect((await fetch(`${url}api/combos/${pieceId}?model=turbo`)).status).toBe(404);
    const updated={...bundledModels,models:bundledModels.models.filter(m=>m.id!=="turbo").map(m=>({...m,characteristics:["Custom speed"]}))};
    await Bun.write(join(root,"resources","orchestra-app.json"),JSON.stringify({music:updated}));
    expect((await (await fetch(`${url}api/models`)).json()).models[0].characteristics).toEqual(["Custom speed"]);
    expect((await withModel("turbo")).status).toBe(400);
    expect((await fetch(`${url}api/combine/missing`)).status).toBe(404);
    expect((await fetch(`${url}api/combine`, { method: "POST", body: "broken" })).status).toBe(400);
  } finally {
    if (server) { server.kill(); await server.exited; }
    await rm(root, { recursive: true, force: true });
  }
}, 15_000);
