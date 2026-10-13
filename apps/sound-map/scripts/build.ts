// Build the server and its HTML/CSS/JavaScript before switching the production entrypoint.
import { mkdir, mkdtemp, readFile, readdir, rename, rm, symlink } from "node:fs/promises";
import { basename, join, resolve } from "node:path";

const ROOT = resolve(import.meta.dir, "..", "..", "..");
const BUILD = join(ROOT, "data", "output", "sound-map-app");
const releases = join(BUILD, "releases");
await mkdir(releases, { recursive: true });
const release = await mkdtemp(join(releases, "release-"));
const link = join(BUILD, `current-${process.pid}`);
try {
  const result = await Bun.build({
    entrypoints: [join(ROOT, "apps", "sound-map", "server.ts")],
    target: "bun", format: "esm", minify: true, outdir: release,
    define: { WHALES_BUILD_ROOT: JSON.stringify(ROOT), "process.env.NODE_ENV": '"production"' },
  });
  if (!result.success) throw new AggregateError(result.logs, "Production build failed");
  if (!(await Bun.file(join(release, "server.js")).exists())) throw new Error("Build did not produce server.js");
  await Bun.write(join(release, "build.json"), JSON.stringify({ built_at_utc: new Date().toISOString(), bun: Bun.version }, null, 2));
  await symlink(join("releases", basename(release)), link);
  await rename(link, join(BUILD, "current"));
  console.log(`Production build: ${result.outputs.length} files, ${(result.outputs.reduce((bytes, output) => bytes + output.size, 0) / 1e6).toFixed(2)} MB → ${BUILD}/current/server.js`);
} catch (error) {
  await rm(link, { force: true });
  await rm(release, { recursive: true, force: true });
  throw error;
}

// Retain the previous release and whichever release the managed server is still serving.
// With no managed state, keep releases: a manually started server may still need them.
const state = await readFile(join(ROOT, "data", "interim", "orchestra", "server.json"), "utf8")
  .then((text) => JSON.parse(text) as { build_dir?: string }).catch(() => null);
if (state?.build_dir) {
  const directories = await readdir(releases);
  const dated = await Promise.all(directories.map(async (name) => {
    const path = join(releases, name);
    const info = await Bun.file(join(path, "build.json")).json().catch(() => null) as { built_at_utc?: string } | null;
    return { path, date: info?.built_at_utc ?? "" };
  }));
  dated.sort((a, b) => b.date.localeCompare(a.date));
  for (const old of dated.slice(2)) {
    if (old.date && old.path !== state.build_dir) await rm(old.path, { recursive: true, force: true });
  }
}
