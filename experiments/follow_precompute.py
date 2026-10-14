"""Precompute unordered ensembles with one model load and resumable checkpoints.

Preview or run every trio on the local GPU:
    uv run --group audio main.py follow precompute-trios --dry-run
    uv run --group audio main.py follow precompute-trios
Repeat --source to select recordings; rerun the same command after interruption.
"""

from itertools import combinations
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import shutil
import subprocess
import time

from .data import Layout, ROOT, now, read_json, write_json
from .follow import ACE_DIR, RUNNER, directories, run_ace
from .follow_combine import RENDER_VERSION, ace_job, catalog, combine, plan, prepared_sources, saved_render
from .follow_models import STABLE_DIR, generation_paths, model_fingerprint, music_model


def combination_specs(config: dict, prepared: dict, size: int, only=None):
    if not 2 <= size <= config.get("combination", {}).get("max_parts", 8):
        raise ValueError("Choose an ensemble size between 2 and the configured maximum")
    ids = {s["id"] for s in config["sources"] if s.get("map") is not False}
    if only:
        unknown = set(only) - ids
        if unknown:
            raise ValueError(f"Unknown map sources: {', '.join(sorted(unknown))}")
        ids &= set(only)
    missing = ids - prepared.keys()
    if missing:
        raise ValueError(f"Unprepared sources: {', '.join(sorted(missing))}; run 'main.py follow prepare' first")
    if len(ids) < size:
        minimum = "two" if size == 2 else str(size)
        raise ValueError(f"Choose at least {minimum} prepared map sources")
    return [{"arrangement": "layer", "gap_s": 1,
             "parts": [{"source": source, "offset_s": 0} for source in ensemble], "ace": {"task": "cover"}}
            for ensemble in combinations(sorted(ids), size)]


def duo_specs(config: dict, prepared: dict, only=None):
    return combination_specs(config, prepared, 2, only)


def local_base_environment(layout: Layout, ace_root: Path | None):
    ace_root = (ace_root or layout.interim / ACE_DIR).resolve()
    python = ace_root / ".venv" / "bin" / "python"
    if not python.exists():
        raise ValueError(f"No ACE-Step environment at {ace_root}; run 'uv run start' to set it up")
    return ace_root, python


def run_local_base(layout, config, jobs, jobs_path, results_path, ace_root, offload):
    """Keep all jobs in one local runner, regardless of the web app's HF/CPU settings."""
    ace_root, python = local_base_environment(layout, ace_root)
    write_json(jobs_path, {"ace_root": str(ace_root), "model": config["ace_step"]["model"], "device": "cuda",
                           "offload": offload, "quantization": "int8_weight_only", "jobs": jobs,
                           "main_repo": config["ace_step"].get("main_repo"),
                           "model_repo": config["ace_step"].get("model_repo"),
                           "main_revision": config["ace_step"].get("main_revision"),
                           "model_revision": config["ace_step"].get("model_revision"),
                           "results": str(results_path.resolve())})
    print(f"{config['ace_step']['model']}: one local GPU model load for {len(jobs)} music jobs", flush=True)
    started = time.perf_counter()
    subprocess.run([str(python), str(RUNNER), str(jobs_path.resolve())], cwd=ace_root, check=True)
    print(f"ACE-Step: {len(jobs)} jobs in {time.perf_counter() - started:.0f} s", flush=True)
    return read_json(results_path)


def local_stable_environment(layout):
    root = (layout.interim / STABLE_DIR).resolve()
    python = root / ".venv/bin/python"
    if not python.exists():
        raise ValueError(f"No Stable Audio environment at {root}; run 'uv run start' to set it up")
    return root, python


def run_local_stable(layout, model, jobs, jobs_path, results_path, ace_root=None, offload=False):
    root, python = local_stable_environment(layout)
    write_json(jobs_path, {"root": str(ROOT), "model": model, "device": "cuda", "jobs": jobs,
                           "results": str(results_path.resolve())})
    print(f"{model['label']}: one local GPU model load for {len(jobs)} music jobs", flush=True)
    subprocess.run([str(python), str(Path(__file__).with_name("follow_stable.py")), str(jobs_path.resolve())], cwd=root, check=True)
    return read_json(results_path)


def render_piece(args):
    layout, config, spec, finalize, model_variant = args
    if finalize:
        combo = plan(spec, config, prepared_sources(layout), model_variant=model_variant)
        folder = directories(layout)[1] / "combos" / combo["id"]
        paths = generation_paths(folder, combo["ace"]["seed"], model_variant, music_model(model_variant))
        if not paths["wav"].exists() or not paths["results"].exists():
            raise ValueError(f"Missing generated music in {folder}; rerun the batch to resume")
    return combine(layout, config, spec, run_model=finalize, model_variant=model_variant, guide_only=not finalize)


def matching_base_job(results: dict, expected: dict, checkpoint: str):
    """Accept cached music only when its model and effective generation settings match."""
    if results.get("model", checkpoint) != checkpoint:
        return None
    job = results.get("jobs", {}).get(expected["id"])
    keys = ("task", "track", "caption", "duration", "seed", "audio_cover_strength", "inference_steps", "guidance_scale")
    if job is None or job.get("model", checkpoint) != checkpoint or job.get("variant", "base") != "base":
        return None
    if any(job.get(key) != expected[key] for key in keys):
        return None
    return job


def restore_base_audio(paths: dict, job: dict, checkpoint: str, previous: dict, legacy_previous: dict):
    """Restore interrupted jobs and copy legacy Base audio without removing old playback files."""
    own_results = paths["results"]
    if paths["wav"].exists():
        if own_results.exists() and matching_base_job(read_json(own_results), job, checkpoint):
            return True
        cached = matching_base_job(previous, job, checkpoint)
        if cached is not None:
            write_json(own_results, previous | {"jobs": {job["id"]: cached | {"output": job["output"]}}})
            return True
    folder = paths["wav"].parent
    legacy_wav, legacy_results = folder / "ace.wav", folder / "ace-results.json"
    if not legacy_wav.exists():
        return False
    old = read_json(legacy_results) if legacy_results.exists() else legacy_previous
    cached = matching_base_job(old, job, checkpoint)
    if cached is None:
        return False
    if not legacy_results.exists():
        write_json(legacy_results, old | {"jobs": {job["id"]: cached}})
    shutil.copy2(legacy_wav, paths["wav"])
    write_json(own_results, old | {"jobs": {job["id"]: cached | {"output": job["output"]}}})
    return True


def restore_model_audio(paths, expected, model, model_variant, previous):
    """Reuse only an exact job from this model/settings checkpoint, preserving other versions."""
    if not paths["wav"].exists():
        return False
    sources = [read_json(paths["results"])] if paths["results"].exists() else []
    sources.append(previous)
    keys = ("task", "track", "caption", "duration", "seed", "audio_cover_strength", "inference_steps", "guidance_scale")
    for results in sources:
        job = results.get("jobs", {}).get(expected["id"])
        if not job or results.get("model", model["checkpoint"]) != model["checkpoint"]:
            continue
        if job.get("model", model["checkpoint"]) != model["checkpoint"] or job.get("variant", model_variant) != model_variant:
            continue
        if any(job.get(key) != expected[key] for key in keys):
            continue
        if model["engine"] == "stable-audio-3" and job.get("parameters", {}).get("init_noise_level") != model.get("init_noise_level", .6):
            continue
        write_json(paths["results"], results | {"jobs": {expected["id"]: job | {"output": expected["output"]}}})
        return True
    return False


def render_pieces(layout, config, specs, finalize, workers, model_variant="base"):
    args = ((layout, config, spec, finalize, model_variant) for spec in specs)
    if workers == 1:
        yield from map(render_piece, args)
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            yield from pool.map(render_piece, args)


def precompute_duos(layout: Layout, config: dict, only=None, ace_root: Path | None = None, offload: bool = False, workers: int = 1, model_variant="base"):
    return precompute_combinations(layout, config, 2, only, ace_root, offload, workers, model_variant=model_variant)


def precompute_trios(layout: Layout, config: dict, only=None, ace_root: Path | None = None, offload: bool = False,
                     workers: int = 4, dry_run: bool = False, model_variant="base"):
    return precompute_combinations(layout, config, 3, only, ace_root, offload, workers, local=True, dry_run=dry_run, model_variant=model_variant)


def precompute_combinations(layout: Layout, config: dict, size: int | list[int], only=None, ace_root: Path | None = None,
                            offload: bool = False, workers: int = 1, *, local: bool = False, dry_run: bool = False, model_variant="base"):
    if workers < 1:
        raise ValueError("Choose at least one CPU worker")
    interim, output = directories(layout)
    prepared = prepared_sources(layout)
    sizes = sorted(set([size] if isinstance(size, int) else size))
    if not sizes:
        raise ValueError("Choose at least one ensemble size")
    specs = [spec for count in sizes for spec in combination_specs(config, prepared, count, only)]
    combos = [plan(spec, config, prepared, model_variant=model_variant) for spec in specs]
    # Freeze each resolved prompt so later display/config edits cannot change prepared piece ids.
    specs = [spec | {"ace": spec["ace"] | {"caption": combo["ace"]["caption"]}} for spec, combo in zip(specs, combos)]
    model = music_model(model_variant)
    model_config = config | {"ace_step": config["ace_step"] | {
        "model": model["checkpoint"], "inference_steps": model["steps"], "guidance_scale": model["guidance_scale"],
    }}
    labels = {count: "duos" if count == 2 else "trios" if count == 3 else f"ensembles-{count}" for count in sizes}
    label = "-".join(labels.values())
    batch_paths = generation_paths(interim / "batches" / label, config["seed"], model_variant, model)
    results_path = batch_paths["results"]
    previous = read_json(results_path) if results_path.exists() else {"jobs": {}}
    legacy_results = interim / f"{label}-ace-results.json"
    legacy_previous = read_json(legacy_results) if legacy_results.exists() else {"jobs": {}}
    jobs = []
    own_results = {}
    unfinished = [(spec, combo) for spec, combo in zip(specs, combos)
                  if not saved_render(output / "combos" / combo["id"], output, needs_ace=True, model_variant=model_variant)]
    print(f"{len(specs)} {label}: {len(specs) - len(unfinished)} finished, {len(unfinished)} pending; "
          f"all players start at 0 s, seed {config['seed']}", flush=True)
    if dry_run:
        print("Dry run: no files written or model loaded", flush=True)
        return None
    if unfinished:
        if model["engine"] == "stable-audio-3":
            local_stable_environment(layout)
        elif local:
            local_base_environment(layout, ace_root)
    pending_specs = [spec for spec, _ in unfinished]
    for index, (_, (_, combo)) in enumerate(zip(render_pieces(layout, config, pending_specs, False, workers, model_variant), unfinished), 1):
        folder = output / "combos" / combo["id"]
        paths = generation_paths(folder, combo["ace"]["seed"], model_variant, model)
        job = ace_job(combo, model_config, folder) | {"output": str(paths["wav"].resolve())}
        own_results[job["id"]] = paths["results"]
        restored = (restore_base_audio(paths, job, model["checkpoint"], previous, legacy_previous) if model_variant == "base"
                    else restore_model_audio(paths, job, model, model_variant, previous))
        if not restored:
            jobs.append(job)
        if index % 10 == 0 or index == len(unfinished):
            print(f"Guides ready: {index}/{len(unfinished)}; {len(jobs)} music jobs pending", flush=True)
    if jobs:
        runner = run_local_stable if model["engine"] == "stable-audio-3" else run_local_base if local else run_ace
        results = runner(layout, model if model["engine"] == "stable-audio-3" else model_config, jobs, batch_paths["jobs"], results_path, ace_root, offload)
        for job in jobs:
            write_json(own_results[job["id"]], {**results, "jobs": {job["id"]: results["jobs"][job["id"]]}})
    for index, _ in enumerate(render_pieces(layout, config, pending_specs, True, workers, model_variant), 1):
        if index % 10 == 0 or index == len(unfinished):
            print(f"Playback files ready: {index}/{len(unfinished)}", flush=True)
    catalog(layout, config)
    index_paths = []
    for count, name in labels.items():
        index_path = output / f"{name}-{model_variant}.json"
        index = {"created_at_utc": now(), "config_sha256": config["config_sha256"], "render_version": RENDER_VERSION,
                 "model_id": model_variant, "model_config_sha256": model_fingerprint(model),
                 "pieces": [{"id": c["id"], "spec": c} for c in combos if len(c["parts"]) == count]}
        write_json(index_path, index)
        if model_variant == "base":
            index_path = output / f"{name}.json"
            write_json(index_path, index)
        index_paths.append(index_path)
    print(f"All {len(combos)} {label} ready → {', '.join(str(path) for path in index_paths)}", flush=True)
    return index_paths[0] if len(index_paths) == 1 else index_paths
