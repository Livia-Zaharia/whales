"""Authenticated ZeroGPU requests and a cancellable HF / local CPU race.

Only the first complete, successful candidate publishes output files. Both
runners write in separate scratch directories, so the loser cannot overwrite
the winning audio or metadata. This module also provides the app's runtime probe
without requiring PyTorch in the main project environment.
"""

import contextlib
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from huggingface_hub import get_token

ROOT = Path(__file__).resolve().parents[1]


def backend_settings():
    space = os.environ.get("ORCHESTRA_HF_SPACE", "").strip()
    backend = os.environ.get("ORCHESTRA_ACE_BACKEND", "auto").strip()
    if backend not in {"auto", "local", "cpu"}:
        raise ValueError("ORCHESTRA_ACE_BACKEND must be auto, local or cpu")
    if space and not re.fullmatch(r"[\w.-]+/[\w.-]+", space):
        raise ValueError("ORCHESTRA_HF_SPACE must be a Hugging Face owner/space id")
    timeout = float(os.environ.get("ORCHESTRA_HF_TIMEOUT", "900"))
    if not 1 <= timeout <= 7200:
        raise ValueError("ORCHESTRA_HF_TIMEOUT must be between 1 and 7200 seconds")
    return space, backend, timeout


def hf_json(url: str, timeout: float = 3):
    token = get_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with urlopen(Request(url, headers=headers), timeout=timeout) as response:
        return json.load(response)


def space_status(space: str | None = None):
    if space is None:
        space, backend, _ = backend_settings()
        if backend != "auto":
            return {"space": space or None, "available": False, "stage": "DISABLED"}
    if not space:
        return {"space": None, "available": False, "stage": "UNCONFIGURED"}
    status = {"space": space, "available": False, "stage": "UNREACHABLE"}
    try:
        info = hf_json(f"https://huggingface.co/api/spaces/{space}")
        stage = info.get("runtime", {}).get("stage", "UNKNOWN")
        host = info.get("host") or f"https://{space.replace('/', '-').replace('_', '-')}.hf.space"
        status |= {"stage": stage, "host": host, "available": stage in {"RUNNING", "RUNNING_BUILDING"}}
        # HF's quota API reports included GPU seconds as `current`. Older Hub
        # SDKs do not yet wrap this endpoint. Never spend prepaid credits here.
        try:
            quota = hf_json("https://huggingface.co/api/spaces/zero-gpu/quota")
            status["quota_remaining_s"] = quota["current"]
            if quota["current"] <= 0:
                status |= {"available": False, "stage": "QUOTA_EXHAUSTED"}
        except HTTPError as error:
            # Readiness still works without billing-read access; remote_jobs
            # requires a known included quota before requesting a GPU.
            if error.code not in {401, 403, 404}:
                raise
    except (OSError, ValueError, KeyError):
        status["available"] = False
    return status


def local_runtime():
    local = {"cuda_available": False, "device": None}
    python = ROOT / "data/interim/tools/ACE-Step-1.5/.venv/bin/python"
    if python.exists():
        try:
            process = subprocess.run([str(python), str(ROOT / "experiments/follow_ace.py"), "--runtime"],
                                     capture_output=True, text=True, timeout=4)
            if process.returncode == 0:
                local = json.loads(process.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return local


def runtime_info():
    space, backend, _ = backend_settings()
    # The app has a ten-second connection check. HF and PyTorch probes are
    # independent and run together so an offline Hub cannot delay local status.
    with ThreadPoolExecutor(max_workers=2) as pool:
        local_probe = pool.submit(local_runtime)
        remote = space_status(space) if space and backend == "auto" else {}
        local = local_probe.result()
    active = bool(remote.get("available"))
    wakeable = remote.get("stage") in {"SLEEPING", "STARTING", "APP_STARTING", "BUILDING",
                                       "RUNNING_BUILDING", "RUNNING_APP_STARTING"}
    return {"ace_available": bool(local["device"] or active or wakeable),
            "cuda_available": True if active else (False if space or backend == "cpu" else local["cuda_available"]),
            "device": "hf" if active else ("cpu" if local["device"] and (space or backend == "cpu") else local["device"]),
            "hf_space": space or None, "hf_available": active, "hf_stage": remote.get("stage"),
            "hf_quota_remaining_s": remote.get("quota_remaining_s")}


def remote_jobs(spec: dict):
    from gradio_client import Client, handle_file
    from orchestra import ACE_COMMIT

    space, _, timeout = backend_settings()
    deadline = time.monotonic() + timeout
    client = None
    print(f"ACE-Step: waking/checking HF Space {space}; CPU continues in parallel", flush=True)
    while client is None:
        status = space_status(space)
        if status["stage"] == "QUOTA_EXHAUSTED":
            raise ValueError("HF included ZeroGPU quota is exhausted")
        if status["stage"] in {"RUNTIME_ERROR", "BUILD_ERROR", "PAUSED", "DELETED"}:
            raise ValueError(f"HF Space is {status['stage'].lower()}")
        if time.monotonic() >= deadline:
            raise TimeoutError("HF Space did not become ready before the timeout")
        if status["available"]:
            try:
                client = Client(space, token=get_token(), verbose=False,
                                httpx_kwargs={"timeout": 5.0},
                                download_files=Path(spec["results"]).parent / "downloads")
                health = client.submit(api_name="/health").result(timeout=5)
                if not health.get("ready") or spec["model"] not in health.get("models", []):
                    raise ValueError("HF Space does not serve the requested model")
                if health.get("ace_git_revision") != ACE_COMMIT:
                    raise ValueError("HF ACE-Step source differs from the local pinned code")
                if spec.get("main_revision") and health.get("main_revision") != spec["main_revision"]:
                    raise ValueError("HF main checkpoint differs from the local pinned checkpoint")
                expected_revision = spec.get("main_revision") if spec["model"] == "acestep-v15-turbo" else spec.get("model_revision")
                if expected_revision and health.get("model_revisions", {}).get(spec["model"]) != expected_revision:
                    raise ValueError("HF music model differs from the local pinned checkpoint")
            except (OSError, TimeoutError):
                if client:
                    client.close()
                client = None
        else:
            # A request to a sleeping Space wakes it. Never block the CPU branch
            # on the cold start or send credentials to an arbitrary host.
            host = status.get("host", "")
            if host.startswith("https://") and host.endswith(".hf.space"):
                with contextlib.suppress(OSError):
                    hf_json(host + "/config")
        if client is None:
            time.sleep(min(3, max(0, deadline - time.monotonic())))
    results = {"jobs": {}, "backend": "hf-zerogpu", "hf_space": space, "model": spec["model"]}
    try:
        for job in spec["jobs"]:
            # Reserve only included time; exhaustion falls back to CPU without
            # consuming prepaid credits. HF also checks quota when allocating.
            required = min(180, max(30, int(20 + float(job["duration"]) * int(job["inference_steps"]) / 25)))
            quota = space_status(space).get("quota_remaining_s")
            if quota is None:
                raise ValueError("HF included GPU quota could not be checked; using local CPU")
            if quota is not None and quota < required:
                raise ValueError("Not enough included HF ZeroGPU time for this job")
            payload = {k: v for k, v in job.items() if k not in {"src_audio", "output"}}
            payload["model"] = spec["model"]
            print("COMPOSE_PROGRESS " + json.dumps({"percent": 35, "stage": "HF ZeroGPU is composing; local CPU is also running…"}), flush=True)
            remote = client.submit(handle_file(job["src_audio"]) if job.get("src_audio") else None,
                                   json.dumps(payload), api_name="/generate")
            try:
                audio, metadata = remote.result(timeout=max(0.1, deadline - time.monotonic()))
            except BaseException:
                with contextlib.suppress(Exception):
                    remote.cancel()
                raise
            if metadata.get("model") != spec["model"]:
                raise ValueError("HF returned a different ACE-Step model")
            output = Path(job["output"])
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(audio, output)
            results["jobs"][job["id"]] = job | metadata | {"hf_space": space}
            results |= {k: metadata[k] for k in ("device", "device_type", "torch", "ace_git_revision") if k in metadata}
        Path(spec["results"]).write_text(json.dumps(results, indent=2) + "\n")
    finally:
        client.close()


def candidate_spec(spec: dict, folder: Path, cpu: bool = False):
    folder.mkdir()
    return spec | {"results": str(folder / "results.json"),
                   "jobs": [job | {"output": str(folder / f"{i}.wav")} for i, job in enumerate(spec["jobs"])],
                   **({"device": "cpu", "offload": False, "quantization": None} if cpu else {})}


def publish_candidate(original: dict, candidate: dict, backend: str):
    """Validate every WAV before publishing; preserve previous batch results."""
    results = json.loads(Path(candidate["results"]).read_text())
    for job in candidate["jobs"]:
        output = Path(job["output"])
        with output.open("rb") as audio:
            header = audio.read(12)
        if header[:4] not in {b"RIFF", b"RF64"} or header[8:12] != b"WAVE" or output.stat().st_size <= 44:
            raise ValueError("Backend did not produce a complete WAV")
        if job["id"] not in results["jobs"]:
            raise ValueError("Backend did not report every job")
    destination = Path(original["results"])
    previous = json.loads(destination.read_text()) if destination.exists() else {"jobs": {}}
    results["jobs"] = previous["jobs"] | results["jobs"]
    for job, staged in zip(original["jobs"], candidate["jobs"]):
        output = Path(job["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        # Same-filesystem replacement also keeps readers from seeing half a WAV.
        temporary = output.with_name(output.name + ".winning")
        shutil.copyfile(staged["output"], temporary)
        temporary.replace(output)
        results["jobs"][job["id"]] |= {"output": job["output"], "backend": backend}
    results |= {"backend": backend, "race_winner": backend}
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_name(destination.name + ".winning")
    pending.write_text(json.dumps(previous | results, indent=2) + "\n")
    pending.replace(destination)


def stop_process(process, already_signalled=False):
    if process.poll() is None:
        try:
            if not already_signalled:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            with contextlib.suppress(OSError):
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            process.wait()


def race_backends(spec: dict, jobs_path: Path, cpu_python: Path, runner: Path):
    events = queue.Queue()
    processes = {}
    candidates = {}
    failures = []
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    previous_handler = None
    if threading.current_thread() is threading.main_thread():
        previous_handler = signal.getsignal(signal.SIGTERM)

        def terminate_race(_signal, _frame):
            # A server shutdown must also reach the detached render processes.
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, terminate_race)

    def watch(name, process):
        with process.stdout:
            for line in process.stdout:
                events.put(("line", name, line.rstrip()))
        events.put(("exit", name, process.wait()))

    with tempfile.TemporaryDirectory(prefix="ace-race-", dir=jobs_path.parent) as scratch:
        try:
            for name, command in [("hf-zerogpu", [sys.executable, "-m", "experiments.follow_hf"]),
                                  ("local-cpu", [str(cpu_python), str(runner)])]:
                if name == "local-cpu" and not cpu_python.exists():
                    failures.append("local-cpu: ACE-Step environment is missing")
                    continue
                candidate = candidate_spec(spec, Path(scratch).resolve() / name, cpu=name == "local-cpu")
                path = Path(scratch).resolve() / f"{name}.json"
                path.write_text(json.dumps(candidate))
                candidates[name] = candidate
                try:
                    process = subprocess.Popen(command + [str(path)], cwd=ROOT if name == "hf-zerogpu" else spec["ace_root"],
                                               env=os.environ | {"PYTHONUNBUFFERED": "1"}, stdout=subprocess.PIPE,
                                               stderr=subprocess.STDOUT, text=True, start_new_session=True)
                except OSError as error:
                    failures.append(f"{name}: {error}")
                    continue
                processes[name] = process
                threading.Thread(target=watch, args=(name, process), daemon=True).start()
            print("ACE-Step: racing HF ZeroGPU and local CPU; the first successful render wins", flush=True)
            pending = set(processes)
            while pending:
                kind, name, value = events.get()
                if kind == "line":
                    # Keep structured progress parseable while distinguishing
                    # logs and nonfatal failures from the two candidates.
                    print(value if value.startswith("COMPOSE_PROGRESS ") else f"ACE-Step [{name}]: {value}", flush=True)
                else:
                    pending.remove(name)
                    if value == 0:
                        try:
                            publish_candidate(spec, candidates[name], name)
                        except (OSError, ValueError, KeyError) as error:
                            failures.append(f"{name}: {error}")
                            continue
                        print(f"ACE-Step: {name} won; stopping the other renderer", flush=True)
                        return
                    failures.append(f"{name}: renderer exited with status {value}")
                    print(f"ACE-Step: {name} stopped; the other renderer continues", flush=True)
            raise ValueError("ACE-Step generation failed on both backends: " + "; ".join(failures))
        finally:
            # Signal both candidates before waiting on either one; a parent
            # shutdown deadline must not leave CPU running during HF cleanup.
            for process in processes.values():
                if process.poll() is None:
                    with contextlib.suppress(OSError):
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGTERM)
                        else:
                            process.terminate()
            try:
                for process in processes.values():
                    stop_process(process, already_signalled=True)
            finally:
                if previous_handler is not None:
                    signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    if sys.argv[1] == "--runtime":
        from orchestra import load_env

        load_env()
        print(json.dumps(runtime_info()))
    else:
        # The parent terminates a losing HF candidate. Cancel its queued/running
        # Gradio request before exiting, so it does not keep consuming GPU time.
        def interrupted(_signal, _frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, interrupted)
        try:
            remote_jobs(json.loads(Path(sys.argv[1]).read_text()))
        except KeyboardInterrupt:
            raise SystemExit(0)
        except Exception as error:
            print(f"HF generation failed: {type(error).__name__}: {error}", flush=True)
            raise SystemExit(1)
