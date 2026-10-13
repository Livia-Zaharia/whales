"""Set up and run the Whale and Dolphin Orchestra, one command each way.

    uv run start   # prepare whatever is missing, then serve the app in the background
    uv run stop    # stop that server

Settings come from .env, which the first start copies from .env.template. Variables
already set in the environment win over .env, and options win over both. Each step
sets up missing dependencies and rebuilds the app on every start. A step that
fails prints a warning with its fix and the start carries on where it can: without
Git LFS the finished demo pieces are missing, and without ACE-Step a piece keeps its
guide and deterministic response. Missing Bun, a failed app build, a busy port or
a server that does not come up stop startup.
"""

from enum import Enum
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import time
from typing import Annotated
from urllib.request import urlopen

import typer
from huggingface_hub.errors import HfHubHTTPError

from experiments.data import ROOT, Layout, now, read_json, write_json
from experiments.follow import ACE_DIR, load_config
from experiments.follow_dataset import download_bundle, prepared_current

APP = ROOT / "apps" / "sound-map"
DEMO = APP / "demo" / "follow"
STATE = ROOT / "data" / "interim" / "orchestra"
SERVER = STATE / "server.json"
LOG = STATE / "server.log"
BUILD = ROOT / "data" / "output" / "sound-map-app"
BUILT_SERVER = BUILD / "current" / "server.js"
ENV, TEMPLATE = ROOT / ".env", ROOT / ".env.template"
# The ACE-Step 1.5 commit every committed demo piece was composed with; its weights are pinned in follow-music.json.
ACE_COMMIT = "ca1e85fe9430179831e6bc6be790c332190a3866"
MIN_BUN = (1, 2, 17)  # ahead-of-time builds of Bun.serve HTML imports
UV = shutil.which("uv") or os.environ.get("UV", "uv")  # `uv run` exports UV even when uv is not on PATH
BASE_PYTHON = [UV, "run"]  # metadata and downloads do not need audio or model dependencies
PYTHON = BASE_PYTHON + ["--group", "audio"]  # the environment server.ts runs `main.py follow` in
WILDCARD = {"0.0.0.0", "::", ""}

# Runs in ACE-Step's own environment: fetch the pinned weights that initialize_service would otherwise
# download unpinned on first use.
WEIGHTS = """
import sys
from pathlib import Path
from huggingface_hub import snapshot_download
from acestep.model_downloader import check_model_exists

root, model, model_repo, model_revision, main_repo, main_revision = sys.argv[1:]
checkpoints = Path(root) / "checkpoints"
components = ["vae", "Qwen3-Embedding-0.6B"]
if not all(check_model_exists(name, checkpoints) for name in components):
    print("  Downloading the ACE-Step VAE and text encoder (about 1.5 GB, once)", flush=True)
    snapshot_download(main_repo, revision=main_revision, local_dir=checkpoints,
                      allow_patterns=[name + "/*" for name in components])
if not check_model_exists(model, checkpoints):
    print(f"  Downloading {model} (about 4.5 GB, once)", flush=True)
    snapshot_download(model_repo, revision=model_revision, local_dir=checkpoints / model)
"""


class Ace(str, Enum):
    auto = "auto"
    yes = "yes"
    no = "no"


class Report:
    """Prints each step and keeps the warnings for a summary at the end."""

    def __init__(self):
        self.warnings: list[str] = []

    def step(self, title: str):
        typer.secho(f"\n▸ {title}", bold=True)

    def ok(self, text: str):
        typer.secho(f"  ✓ {text}", fg="green")

    def note(self, text: str):
        typer.echo(f"  {text}")

    def warn(self, text: str):
        self.warnings.append(text)
        typer.secho(f"  ! {text}", fg="yellow")

    def fail(self, text: str):
        typer.secho(f"  ✗ {text}", fg="red", err=True)
        self.summary()
        raise typer.Exit(1)

    def summary(self):
        if self.warnings:
            typer.secho(f"\n{len(self.warnings)} warning(s):", fg="yellow", bold=True)
            for text in self.warnings:
                typer.secho(f"  - {text}", fg="yellow")


# --- Settings and tools ---------------------------------------------------------------------

def load_env():
    """Put .env (before the first start, the template's defaults) into os.environ; set variables win."""
    path = ENV if ENV.exists() else TEMPLATE
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().removeprefix("export ").partition("=")
        if not sep or key.startswith("#"):
            continue
        value = value.strip()
        quoted = len(value) > 1 and value[0] == value[-1] and value[0] in "'\""
        value = value[1:-1] if quoted else value.split(" #", 1)[0].strip()
        if value:
            os.environ.setdefault(key.strip(), value)
    if (home := os.environ.get("HF_HOME")) and not Path(home).is_absolute():
        os.environ["HF_HOME"] = str(ROOT / home)  # ACE-Step runs from its own folder


def env_keys(path: Path, commented: bool = True) -> set[str]:
    """Keys a .env-style file sets, and with `commented` also those it shows commented out."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pattern = r"\s*#?\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=" if commented else r"\s*(?:export\s+)?([A-Z][A-Z0-9_]*)="
    return {match[1] for line in lines if (match := re.match(pattern, line))}


def output(*command: str) -> str | None:
    """What a command prints, or None when it is missing or fails."""
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def run(command: list[str], cwd: Path = ROOT, env: dict | None = None) -> bool:
    """Run with its output streaming to the terminal; True on success."""
    try:
        return subprocess.run(command, cwd=cwd, env=env).returncode == 0
    except OSError as error:
        typer.secho(f"  {error}", fg="red")
        return False


def tools(report: Report) -> str:
    report.step("Tools")
    bun = shutil.which("bun")
    if not bun:
        report.fail("Bun is not installed or not on PATH, and the app runs on it. Install it with "
                    "`curl -fsSL https://bun.sh/install | bash`, open a new shell and run `uv run start` again.")
    bun_version = output(bun, "--version")
    if bun_version is None:
        report.fail(f"`{bun} --version` fails, so this Bun is broken. Reinstall it from https://bun.sh.")
    found = [f"Bun {bun_version}"]
    if tuple(int(n) for n in re.findall(r"\d+", bun_version)[:3]) < MIN_BUN:
        report.warn(f"Bun {bun_version} is older than 1.2.17, which the production build needs. Run `bun upgrade`.")
    if uv := output(UV, "--version"):
        found.append(uv)
    if not shutil.which("uv"):
        report.warn("uv is not on PATH. The server calls `uv run` to compose pieces, so composing will fail. "
                    "Add uv's folder (usually ~/.local/bin) to PATH.")
    if git := output("git", "--version"):
        found.append(git.replace("git version", "git"))
    else:
        report.warn("Git is not installed, so there are no demo pieces from Git LFS and no ACE-Step checkout.")
    report.ok(", ".join(found))
    return bun


# --- Setup steps ----------------------------------------------------------------------------

def placeholders() -> list[Path]:
    """Demo files that still hold a Git LFS pointer instead of their content."""
    found = []
    for path in DEMO.rglob("*"):
        if path.is_file():
            with path.open("rb") as handle:
                if handle.read(32).startswith(b"version https://git-lfs"):
                    found.append(path)
    return found


def demo_bundle(report: Report):
    report.step("Demo bundle: recordings and finished pieces (Git LFS)")
    missing = placeholders()
    if not missing:
        report.ok("present")
        return
    if not (ROOT / ".git").exists():
        report.warn(f"This folder is not a Git clone, so the {len(missing)} demo files cannot be fetched and the finished "
                    "demo pieces will not play. Clone the repository with Git instead of downloading an archive.")
        return
    if output("git", "lfs", "version") is None:
        report.warn(f"Git LFS is not installed, so {len(missing)} demo files are placeholders and the finished demo pieces "
                    "will not play. Install git-lfs (for example `sudo apt install git-lfs`) and run `uv run start` again.")
        return
    run(["git", "lfs", "pull", "--include", "apps/sound-map/demo/**"])
    if left := placeholders():
        report.warn(f"{len(left)} demo files are still Git LFS placeholders (see the output above), "
                    "so the demo pieces they belong to will not play.")
    else:
        report.ok(f"downloaded {len(missing)} files")


def packages(report: Report, bun: str):
    report.step("App packages (bun install)")
    if run([bun, "install"], cwd=APP):
        report.ok("installed")
    else:
        report.warn("`bun install` failed (output above), so the server may not start. Check the network, or delete "
                    "apps/sound-map/node_modules and run `uv run start` again.")


def build_app(report: Report, bun: str):
    report.step("Production app (bun run build)")
    if not run([bun, "run", "build"], cwd=APP):
        report.fail("The production build failed (output above). Any running server keeps its previous build. "
                    "Fix the build and run `uv run start` again.")
    if not BUILT_SERVER.is_file():
        report.fail("The build did not produce the production server.")
    report.ok("server and frontend bundled before startup")


def recordings(report: Report, layout: Layout):
    report.step("Prepared recordings and measured phrases")
    config = load_config()
    catalog = layout.output / "follow" / "catalog.json"
    ids = {source["id"] for source in config["sources"]}
    measured = prepared_current(layout, config)
    try:
        catalog_current = read_json(catalog)["config_sha256"] == config["config_sha256"]
    except (OSError, ValueError, KeyError):
        catalog_current = False
    if measured and catalog_current:
        report.ok(f"{len(ids)} recordings measured, catalog up to date")
        return
    if not measured:
        report.note("Downloading the pinned Hugging Face bundle: Parquet measurements and lossless FLAC clips.")
        try:
            download_bundle(layout, config)
        except (OSError, ValueError, KeyError, HfHubHTTPError) as error:
            report.warn(f"Prepared dataset could not load: {error}. The app keeps its existing catalog or plays the demo bundle. "
                        "Startup does not download source corpora; retry with `main.py follow dataset download`.")
            return
        catalog_current = read_json(catalog)["config_sha256"] == config["config_sha256"]
    if not catalog_current and not run(PYTHON + ["main.py", "follow", "catalog", "--data-dir", str(layout.root)]):
        report.warn("Refreshing the catalog failed. Prepared recordings are retained; run `main.py follow catalog` to retry.")
        return
    report.ok(f"{len(ids)} recordings measured, catalog written")


def gpu() -> tuple[str, int, int] | None:
    """Name, used MiB and total MiB of the first NVIDIA GPU, or None without one."""
    text = output("nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader,nounits")
    if not text:
        return None
    name, used, total = (part.strip() for part in text.splitlines()[0].rsplit(",", 2))
    try:
        return name, int(used), int(total)
    except ValueError:
        return name, 0, 0


def ace_step(report: Report, mode: Ace, layout: Layout):
    report.step("ACE-Step 1.5, the model that composes the pieces")
    if mode is Ace.no:
        report.ok("skipped (ORCHESTRA_ACE=no): pieces get their guide and deterministic response")
        return
    from experiments.follow_hf import backend_settings

    space, backend, _ = backend_settings()
    # A configured Space always gets a prepared CPU competitor, even on a host
    # with CUDA. That competitor also works when HF is cold or unreachable.
    cpu = backend == "cpu" or bool(space and backend == "auto")
    card = None if cpu else gpu()
    if card is None and mode is Ace.auto and not cpu:
        report.warn("No NVIDIA GPU found (nvidia-smi), so ACE-Step is skipped and pieces get their guide and "
                    "deterministic response only. Set ORCHESTRA_ACE=yes in .env to enable CPU generation.")
        return
    if space and backend == "auto":
        report.note(f"HF ZeroGPU: {space}; preparing local CPU to race it. The first successful result wins.")
    if card is None:
        report.note("No NVIDIA GPU found. ACE-Step will use the CPU; generation is slower than on a GPU.")
    else:
        name, used, total = card
        report.ok(f"{name}: {used / 1024:.1f} of {total / 1024:.1f} GiB in use")
        if total and used > total / 2:
            report.warn(f"The GPU is already {used / total:.0%} full, so another job is using it. Composing may run out "
                        "of memory until that job ends.")
    root = layout.interim / ACE_DIR
    python = root / ".venv" / "bin" / "python"
    settings = load_config()["ace_step"]
    if not root.exists():
        if not shutil.which("git"):
            report.warn("Git is missing, so ACE-Step cannot be cloned. Install git and run `uv run start` again.")
            return
        report.note("First install: clones ACE-Step, builds its own environment and downloads the phrase runner's weights (about 6 GB).")
        root.parent.mkdir(parents=True, exist_ok=True)
        if not (run(["git", "clone", settings["repository"], str(root)])
                and run(["git", "-C", str(root), "checkout", "--quiet", ACE_COMMIT])):
            shutil.rmtree(root, ignore_errors=True)  # only ever a clone this call started
            report.warn("Cloning ACE-Step failed (output above). Check the network and run `uv run start` again.")
            return
    # An existing CUDA environment can run CPU jobs too. Keep it intact when HF
    # racing is requested on a GPU host; install CPU wheels for fresh fallbacks.
    needs_cpu_install = card is None and not (cpu and python.exists())
    if needs_cpu_install or not python.exists():
        env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}  # it keeps its own .venv
        if card is None:
            # Upstream's Linux uv project pins CUDA wheels even on CPU-only servers.
            # Keep its checkout intact and install only the inference stack in its own .venv.
            commands = [] if python.exists() else [[UV, "venv", "--python", "3.12", str(root / ".venv")]]
            commands.append([UV, "pip", "install", "--no-cache", "--python", str(python), "--torch-backend", "cpu",
                             "-r", str(ROOT / "resources" / "ace-cpu-requirements.txt")])
        else:
            commands = [[UV, "sync"]]
        if not all(run(command, cwd=root, env=env) for command in commands) or not python.exists():
            report.warn(f"Installing ACE-Step dependencies failed in {root.relative_to(ROOT)} (output above). Pieces still get their guide and "
                        "deterministic response; run `uv run start` again to retry.")
            return
    if not run([str(python), "-c", WEIGHTS, str(root), settings["model"], settings["model_repo"], settings["model_revision"],
                settings["main_repo"], settings["main_revision"]], cwd=root):
        report.warn("Downloading the ACE-Step weights failed (output above). Run `uv run start` again to resume.")
        return
    revision = output("git", "-C", str(root), "rev-parse", "HEAD") or "unknown"
    report.ok(f"ready in {root.relative_to(ROOT)} ({revision[:7]}, model {settings['model']})")
    if revision != ACE_COMMIT:
        report.note(f"This checkout is at {revision[:7]}; the demo pieces were composed at {ACE_COMMIT[:7]}.")


# --- Server ---------------------------------------------------------------------------------

def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def ours(pid: int) -> bool:
    """True while pid is still the server `start` launched; pids get reused."""
    if not alive(pid):
        return False
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes()
        return b"server.ts" in command or any(
            part.startswith((str(BUILD) + "/").encode()) and part.endswith(b"/server.js")
            for part in command.split(b"\0")
        )
    except FileNotFoundError:
        return not Path("/proc/self").exists()  # no /proc (macOS): trust the record
    except OSError:
        return False


def running() -> dict | None:
    try:
        state = read_json(SERVER)
    except (OSError, ValueError):
        return None
    return state if ours(state["pid"]) else None


def answers(host: str, port: int) -> bool:
    try:
        with socket.create_connection(("localhost" if host in WILDCARD else host, port), timeout=1):
            return True
    except OSError:
        return False


def port_free(report: Report, host: str, port: int):
    if answers(host, port):
        report.fail(f"Port {port} is already in use by another program. Choose a free one with ORCHESTRA_PORT in .env "
                    "or `uv run start --port <port>`.")


def serve(report: Report, bun: str, host: str, port: int) -> str:
    report.step("Server")
    port_free(report, host, port)
    url = f"http://{'localhost' if host in WILDCARD else host}:{port}"
    STATE.mkdir(parents=True, exist_ok=True)
    env = os.environ | {"ORCHESTRA_PORT": str(port), "ORCHESTRA_HOST": host, "NODE_ENV": "production", "WHALES_ROOT": str(ROOT)}
    executable = BUILT_SERVER.resolve()
    with LOG.open("w", encoding="utf-8") as log:  # its own session, so it outlives this command
        server = subprocess.Popen([bun, str(executable)], cwd=executable.parent, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                  stderr=subprocess.STDOUT, start_new_session=True)
    write_json(SERVER, {"pid": server.pid, "url": url, "host": host, "port": port, "log": str(LOG.relative_to(ROOT)),
                        "started_at_utc": now(), "build_dir": str(executable.parent)})
    log_name = LOG.relative_to(ROOT)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if server.poll() is not None:
            SERVER.unlink(missing_ok=True)
            tail = LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
            report.fail(f"The server stopped right after starting (exit {server.returncode}). End of {log_name}:\n    "
                        + "\n    ".join(tail))
        try:
            with urlopen(url + "/", timeout=5):
                break
        except OSError:
            time.sleep(0.5)
    else:
        report.warn(f"The server has not answered within a minute; it may still be starting. Its log is {log_name}.")
        return url
    report.ok(f"{url} (pid {server.pid}, log {log_name})")
    if host in WILDCARD:
        report.note("Listening on every network interface. The server has no login, so keep it behind a firewall or proxy.")
    return url


# --- Commands -------------------------------------------------------------------------------

start_app = typer.Typer(add_completion=False)
stop_app = typer.Typer(add_completion=False)


@start_app.command()
def start(
    port: Annotated[int, typer.Option(envvar="ORCHESTRA_PORT", help="Port for the app.")] = 3070,
    host: Annotated[str, typer.Option(envvar="ORCHESTRA_HOST", help="127.0.0.1 serves this machine, 0.0.0.0 the network.")] = "127.0.0.1",
    ace: Annotated[Ace, typer.Option(envvar="ORCHESTRA_ACE", help="Install ACE-Step 1.5: auto (with NVIDIA GPU or HF Space), yes or no.")] = Ace.auto,
):
    """Set up whatever is missing, then serve the Whale and Dolphin Orchestra in the background."""
    if state := running():
        report = Report()
        bun = tools(report)
        packages(report, bun)
        build_app(report, bun)
        typer.echo(f"The orchestra is already running at {state['url']} (pid {state['pid']}). Stop it with `uv run stop`.")
        report.note("The new build will be used on the next restart; the running server is retained.")
        report.summary()
        return
    report = Report()
    typer.secho("Whale and Dolphin Orchestra", bold=True)
    if not ENV.exists() and TEMPLATE.exists():
        shutil.copyfile(TEMPLATE, ENV)
        report.note("Created .env from .env.template; edit it to change the port, host or ACE-Step setting.")
    elif missing := sorted(env_keys(TEMPLATE, commented=False) - env_keys(ENV)):
        report.note(f"Your .env predates {', '.join(missing)}; defaults apply. Copy the lines from .env.template to change them.")
    report.note(f"Settings: port {port}, host {host}, ACE-Step {ace.value}")
    layout = Layout()
    bun = tools(report)
    port_free(report, host, port)  # before the long steps, not after them
    demo_bundle(report)
    packages(report, bun)
    build_app(report, bun)
    recordings(report, layout)
    ace_step(report, ace, layout)
    url = serve(report, bun, host, port)
    report.summary()
    typer.secho(f"\nOpen {url}   ·   stop it with `uv run stop`", bold=True)


@stop_app.command()
def stop():
    """Stop the server that `uv run start` launched, with any piece it is composing."""
    try:
        state = read_json(SERVER)
    except (OSError, ValueError):
        state = None
    if state is None or not ours(state["pid"]):
        SERVER.unlink(missing_ok=True)
        port, host = int(os.environ.get("ORCHESTRA_PORT", 3070)), os.environ.get("ORCHESTRA_HOST", "127.0.0.1")
        if answers(host, port):
            typer.secho(f"Something answers on port {port}, but `uv run start` did not start it (perhaps `bun run dev` "
                        "in a terminal). Stop it where it runs.", fg="yellow")
            raise typer.Exit(1)
        typer.echo("The orchestra is not running.")
        return
    pid = state["pid"]
    try:
        os.killpg(pid, signal.SIGTERM)  # the server leads its own process group, which includes its Python jobs
        deadline = time.monotonic() + 10
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        if alive(pid):
            typer.secho("The server did not exit within 10 s; killing it.", fg="yellow")
            os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    SERVER.unlink(missing_ok=True)
    typer.secho(f"Stopped the orchestra at {state['url']}.", fg="green")


def start_main():
    load_env()
    start_app()


def stop_main():
    load_env()
    stop_app()
