import { expect, test } from "bun:test";
import { mkdir, mkdtemp, readFile, realpath, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

test("a failed production build keeps the previous release; a successful build switches it", async () => {
  const root = await mkdtemp(join(tmpdir(), "whales-build-test-"));
  try {
    const app = join(root, "apps", "sound-map");
    const build = join(root, "data", "output", "sound-map-app");
    const previous = join(build, "releases", "previous");
    await mkdir(join(app, "scripts"), { recursive: true });
    await mkdir(previous, { recursive: true });
    await writeFile(join(previous, "server.js"), "previous working server");
    await symlink(join("releases", "previous"), join(build, "current"));
    await writeFile(join(app, "scripts", "build.ts"), await readFile(new URL("../scripts/build.ts", import.meta.url)));
    await writeFile(join(app, "server.ts"), "const = broken syntax;");
    const failed = Bun.spawn([process.execPath, join(app, "scripts", "build.ts")], { stdout: "ignore", stderr: "ignore" });
    expect(await failed.exited).not.toBe(0);
    expect(await realpath(join(build, "current"))).toBe(previous);
    expect(await readFile(join(build, "current", "server.js"), "utf8")).toBe("previous working server");
    await writeFile(join(app, "server.ts"), 'console.log("new built server");');
    const succeeded = Bun.spawn([process.execPath, join(app, "scripts", "build.ts")], { stdout: "ignore", stderr: "ignore" });
    expect(await succeeded.exited).toBe(0);
    expect(await realpath(join(build, "current"))).not.toBe(previous);
    expect(await readFile(join(build, "current", "server.js"), "utf8")).toContain("new built server");
    expect(await readFile(join(previous, "server.js"), "utf8")).toBe("previous working server");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
