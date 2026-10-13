import { realpath } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";

const ROOT = resolve(import.meta.dir, "..", "..", "..");
const server = await realpath(join(ROOT, "data", "output", "sound-map-app", "current", "server.js"));
const child = Bun.spawn([process.execPath, `--env-file=${join(ROOT, ".env")}`, server], {
  cwd: dirname(server), env: process.env, stdin: "inherit", stdout: "inherit", stderr: "inherit",
});
for (const signal of ["SIGINT", "SIGTERM"] as const) process.on(signal, () => child.kill(signal));
process.exitCode = await child.exited;
