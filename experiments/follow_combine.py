"""Combine several measured phrases into one piece, and place them on the map.

A combination spec names sources and how they sit in time: layered at chosen
offsets, or in sequence with a gap. Each part may be trimmed and given a gain and
register. Every part keeps its own measured events, instrument and register; the
recordings, guides and deterministic response are summed, then one gain scales the
whole combination so the declared balance survives. ACE-Step can then cover or
accompany the combined guide. Timing is scored against every part separately, so
a layered combination shows which phrase the music follows.

The sound map (apps/sound-map) writes specs, runs `main.py follow combine`, and
reads the manifest this module writes. Python stays the only implementation of
measurement, generation and scoring.
"""

import hashlib
import json
import math
from pathlib import Path
import shutil
import tempfile

from .data import Layout, ROOT, now, read_json, write_json
from .follow import contour_at, directories, onsets, respond, render_guide, run_ace, write_wav
from .follow_gallery import MUSIC_BAND, event_data, listening
from .follow_metrics import leakage, load_mono, onset_envelope, timing
from .follow_models import model_registry, music_model, model_fingerprint, rendered_model, run_stable, generation_paths
from .follow_prompt import ensemble_caption

WORLD = ROOT / "resources" / "maps" / "world-map-data.json"
COUNTRIES = ROOT / "resources" / "maps" / "world-countries-110m.geojson"
ARRANGEMENTS = ("layer", "sequence")
TASKS = ("cover", "lego")
# Bump when rendering, response or scoring code changes: saved renders with another version are rebuilt
# on their next request. Ids hash the spec and config, not the code. Manifests without the field are version 1.
RENDER_VERSION = 8


def report_progress(percent: float, stage: str):
    """Report completed pipeline stages without changing the rendered piece."""
    print("COMPOSE_PROGRESS " + json.dumps({"percent": round(percent, 1), "stage": stage}), flush=True)


def prepared_sources(layout: Layout):
    interim, _ = directories(layout)
    path = interim / "prepared.json"
    if not path.exists():
        raise ValueError("No prepared sources; run 'main.py follow prepare' first")
    return {item["id"]: item for item in read_json(path)["sources"]}


def catalog(layout: Layout, config: dict):
    """catalog.json: located sources with events and listening files, plus the dataset points for context."""
    _, output = directories(layout)
    prepared = prepared_sources(layout)
    rate = config["sample_rate"]
    sources = []
    for source in config["sources"]:
        item = prepared.get(source["id"])
        if item is None or source.get("map") is False:  # "map": false keeps a source for the study only
            continue
        species = config["species"][item["species"]]
        files = {}
        for name in ("animal", "guide"):
            band = species["spectrogram_hz"] if name == "animal" else MUSIC_BAND
            audio, image = listening(output / item["stems"][name], band, rate)
            files[name] = {"audio": str(audio.relative_to(output)), "image": str(image.relative_to(output)), "band": list(band)}
        sources.append({"id": item["id"], "species": item["species"], "title": item["title"], "note": item["note"],
                        "duration_s": round(item["duration_s"], 3), "location": source["location"], "material": species["material"],
                        "instrument": species["instrument"]["name"], "register_shift_octaves": item["register_shift_octaves"],
                        "events": event_data(item["events"]), "onsets_s": [round(t, 4) for t in item["onsets_s"]], "files": files})
    world = read_json(WORLD)
    points = [{key: point.get(key) for key in ("id", "label", "dataset", "lon", "lat", "kind", "species", "source", "note")}
              for point in world["points"]]
    shutil.copyfile(COUNTRIES, output / "world-countries.geojson")
    data = {"created_at_utc": now(), "config_sha256": config["config_sha256"], "render_version": RENDER_VERSION,
            "sources": sources, "points": points, "countries": "world-countries.geojson",
            "combination": config["combination"], "seed": config["seed"],
            "species": {name: {"caption": s["caption"], "instrument": s["instrument"]["name"], "material": s["material"],
                               "lego_track": s["lego_track"]} for name, s in config["species"].items()}}
    write_json(output / "catalog.json", data)
    return output / "catalog.json"


def finite(value, name: str):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def plan(spec: dict, config: dict, prepared: dict):
    """Validate a spec and resolve every part's trim, offset, gain and register; the id hashes the result."""
    limits = config["combination"]
    arrangement = spec.get("arrangement", "layer")
    if arrangement not in ARRANGEMENTS:
        raise ValueError(f"arrangement must be one of {', '.join(ARRANGEMENTS)}")
    parts = spec.get("parts") or []
    if not 1 <= len(parts) <= limits["max_parts"]:
        raise ValueError(f"Give between 1 and {limits['max_parts']} parts")
    gap = finite(spec.get("gap_s", 1.0), "gap_s")
    if not 0 <= gap <= 30:
        raise ValueError("gap_s must be between 0 and 30 seconds")
    low_gain, high_gain = limits["gain_db"]
    resolved, clock = [], 0.0
    for index, part in enumerate(parts):
        source = part.get("source")
        if source not in prepared:
            raise ValueError(f"Part {index + 1}: unknown or unprepared source {source!r}")
        item = prepared[source]
        start, end = (finite(v, f"part {index + 1} trim") for v in (part.get("trim_s") or [0, item["duration_s"]]))
        start, end = max(0.0, start), min(item["duration_s"], end)
        if end - start < limits["min_part_s"]:
            raise ValueError(f"Part {index + 1}: trim must keep at least {limits['min_part_s']} s of {source}")
        offset = clock if arrangement == "sequence" else finite(part.get("offset_s", 0.0), f"part {index + 1} offset")
        if offset < 0:
            raise ValueError(f"Part {index + 1}: offset must not be negative")
        gain = finite(part.get("gain_db", 0.0), f"part {index + 1} gain")
        if not low_gain <= gain <= high_gain:
            raise ValueError(f"Part {index + 1}: gain must be between {low_gain} and {high_gain} dB")
        shift = part.get("shift_octaves")
        shift = item["register_shift_octaves"] if shift is None else int(shift)
        if abs(shift) > 6:
            raise ValueError(f"Part {index + 1}: register shift must be within ±6 octaves")
        resolved.append({"index": index, "source": source, "species": item["species"], "trim_s": [round(start, 3), round(end, 3)],
                         "offset_s": round(offset, 3), "gain_db": round(gain, 2), "shift_octaves": shift})
        clock = offset + end - start + gap
    if arrangement == "layer":
        # Simultaneous layers are unordered. Keep explicit timing and balance attached to each source.
        resolved.sort(key=lambda p: (p["source"], p["offset_s"], *p["trim_s"], p["gain_db"], p["shift_octaves"]))
        for index, part in enumerate(resolved):
            part["index"] = index
    duration = round(max(p["offset_s"] + p["trim_s"][1] - p["trim_s"][0] for p in resolved) + .5, 3)
    if duration > limits["max_duration_s"]:
        raise ValueError(f"The combination lasts {duration:.0f} s; the limit is {limits['max_duration_s']} s")
    ace = spec.get("ace")
    if ace:
        species = {p["species"] for p in resolved}
        task = ace.get("task", "cover")
        if task not in TASKS:
            raise ValueError(f"ace.task must be one of {', '.join(TASKS)}")
        single = config["species"][next(iter(species))] if len(species) == 1 else None
        ace = {"task": task, "caption": str(ace.get("caption") or ensemble_caption(species, config))[:400],
               "audio_cover_strength": min(1.0, max(0.0, finite(ace.get("audio_cover_strength", limits["audio_cover_strength"]), "audio_cover_strength"))),
               "track": ace.get("track") or (single["lego_track"] if single else "keyboard"), "seed": int(ace.get("seed", config["seed"]))}
    combo = {"arrangement": arrangement, "gap_s": gap, "parts": resolved, "duration_s": duration, "ace": ace or None}
    # The config hash is part of the id, so editing instruments or rules never reuses stale stems.
    combo["id"] = hashlib.sha1(json.dumps([combo, config["config_sha256"]], sort_keys=True).encode()).hexdigest()[:12]
    combo["title"] = str(spec.get("title") or " + ".join(p["source"] for p in resolved))[:120]
    return combo


MIN_CUT_S = .15  # the shortest part of a trimmed call that still sounds in the guide


def placed(events: list[dict], trim, offset: float):
    """Events inside the trim, moved to the part's offset.

    A call the trim cuts keeps the part of its contour inside the trim, so the guide sounds wherever the
    recording does. A call cut at its start is marked so it adds no onset.
    """
    start, end = trim
    moved = offset - start
    out = []
    for event in events:
        if event["kind"] == "click":
            if start <= event["time_s"] < end:
                out.append(event | {"time_s": event["time_s"] + moved})
            continue
        if event["start_s"] >= start and event["end_s"] <= end:
            out.append(event | {"start_s": event["start_s"] + moved, "end_s": event["end_s"] + moved,
                                "time_s": [t + moved for t in event["time_s"]]})
            continue
        begin, finish = max(event["start_s"], start), min(event["end_s"], end)
        if finish - begin < MIN_CUT_S:
            continue
        times = [begin] + [t for t in event["time_s"] if begin < t < finish] + [finish]  # the contour, read at the trim's edges too
        cut_start = event["start_s"] < start or event.get("cut") in ("start", "both")
        cut_end = event["end_s"] > end or event.get("cut") in ("end", "both")
        out.append(event | {"start_s": begin + moved, "end_s": finish + moved, "time_s": [t + moved for t in times],
                            "hz": [float(f) for f in contour_at(event, times)],
                            "cut": {(True, False): "start", (False, True): "end", (True, True): "both"}[cut_start, cut_end]})
    return out


def scores(audio, rate: int, parts: list[dict], frame_rate: int, metrics: dict):
    """Timing against all onsets over the whole piece, and against each part only while that part plays."""
    env = onset_envelope(audio, rate, frame_rate)
    sigma, shift = metrics["onset_sigma_s"], metrics["min_null_shift_s"]
    everything = sorted(t for part in parts for t in part["onsets_s"])
    per_part = []
    for part in parts:
        start = round(part["offset_s"] * frame_rate)
        stop = min(len(env), start + round((part["trim_s"][1] - part["trim_s"][0]) * frame_rate))
        window = env[start:stop]
        # Circular shifts need room: at least three times the minimum shift inside the part's span.
        if len(window) < 3 * shift * frame_rate:
            per_part.append(None)
            continue
        window = (window - window.mean()) / (window.std() + 1e-12)
        per_part.append(timing(window, [t - part["offset_s"] for t in part["onsets_s"]], frame_rate, sigma, shift))
    return {"all": timing(env, [t for t in everything if t < len(env) / frame_rate], frame_rate, sigma, shift), "parts": per_part,
            "part_window": "each part is scored only between its offset and its end",
            "envelope": [round(float(v), 2) for v in env[::2]], "envelope_rate_hz": frame_rate / 2}


def saved_render(folder: Path, output: Path, needs_ace: bool, model_variant: str = "base") -> Path | None:
    """The manifest of an earlier render of this exact combination, when every file it lists still exists.

    The id hashes the resolved spec and the experiment config, so a match needs no re-rendering. A render made
    without the model is reused only when this request does not ask for ACE-Step either.
    """
    registry = model_registry()
    model = music_model(model_variant, registry)
    canonical = folder / "manifest.json"
    specific = folder / f"manifest-{model_variant}.json"
    paths = [canonical, specific] if model_variant == registry["primary_model"] else [specific, canonical]
    for path in paths:
        if not path.exists():
            continue
        manifest = read_json(path)
        if manifest.get("render_version", 1) != RENDER_VERSION:
            continue
        if needs_ace:
            saved = rendered_model(manifest)
            if "ace" not in manifest.get("stems", {}) or saved != model_variant:
                continue
            fingerprint = (manifest.get("generation") or {}).get("model_config_sha256")
            if fingerprint and fingerprint != model_fingerprint(music_model(saved, registry)):
                continue
        files = [stem[key] for stem in manifest["stems"].values() for key in ("audio", "image")]
        files += [f for part in manifest["parts"] for f in part["files"].values()]
        if all((output / f).exists() for f in files):
            return path
    return None


def ace_job(combo: dict, config: dict, folder: Path):
    a = combo["ace"]
    return {"id": f"combo-{combo['id']}", "source_id": combo["id"], "condition": a["task"], "task": a["task"],
            "src_audio": str((folder / "guide.wav").resolve()), "track": a["track"] if a["task"] == "lego" else None,
            "caption": a["caption"], "duration": combo["duration_s"], "seed": a["seed"], "audio_cover_strength": a["audio_cover_strength"],
            "inference_steps": config["ace_step"]["inference_steps"], "guidance_scale": config["ace_step"]["guidance_scale"],
            "output": str((folder / "ace.wav").resolve())}


def combine(layout: Layout, config: dict, spec: dict, ace_root: Path | None = None, run_model: bool = True, offload: bool = False,
            reuse: bool = True, model_variant: str | None = None):
    """Write stems, listening files and manifest.json for one combination; returns the manifest path."""
    import numpy as np

    registry = model_registry()
    model_variant = model_variant or registry["default_model"]
    model = music_model(model_variant, registry)
    if model_variant != "base" and (spec.get("ace") or {}).get("task") == "lego":
        raise ValueError("Choose Base for adding a track; this model supports cover composition")
    report_progress(2, "Preparing the selected recordings…")
    _, output = directories(layout)
    prepared = prepared_sources(layout)
    combo = plan(spec, config, prepared)
    if combo["duration_s"] > model.get("max_duration_s", float("inf")):
        raise ValueError(f"{model['label']} supports pieces up to {model['max_duration_s']} seconds. Shorten the piece or choose another model.")
    folder = output / "combos" / combo["id"]
    reused = saved_render(folder, output, bool(combo["ace"] and run_model), model_variant) if reuse else None
    if reused:
        report_progress(99, "Opening the saved composition…")
        print(f"Reusing the saved render → combos/{combo['id']}/manifest.json")
        return reused
    folder.mkdir(parents=True, exist_ok=True)
    rate, n = config["sample_rate"], round(combo["duration_s"] * config["sample_rate"])
    locations = {s["id"]: s["location"] for s in config["sources"]}
    mixes = {name: np.zeros(n) for name in ("animal", "guide", "response")}
    layers, parts = [], []
    for part in combo["parts"]:
        report_progress(5 + 15 * part["index"] / len(combo["parts"]),
                        f"Preparing recording {part['index'] + 1} of {len(combo['parts'])}…")
        item = prepared[part["source"]]
        species = config["species"][item["species"]]
        events = placed(item["events"], part["trim_s"], part["offset_s"])
        gain = 10 ** (part["gain_db"] / 20)
        start, end = (round(t * rate) for t in part["trim_s"])
        recording = load_mono(output / item["stems"]["animal"], rate)[start:end]
        animal = np.zeros(n)
        i = round(part["offset_s"] * rate)
        animal[i:i + len(recording)] = recording[:n - i]
        guide = render_guide(events, combo["duration_s"], rate, species["instrument"], part["shift_octaves"])
        response = respond(events, combo["duration_s"], rate, part["shift_octaves"], config["response"])
        for name, layer in (("animal", animal), ("guide", guide), ("response", response)):
            mixes[name] += gain * layer
        layers.append({"animal": gain * animal, "guide": gain * guide})
        parts.append(part | {"title": item["title"], "location": locations[part["source"]], "instrument": species["instrument"]["name"],
                             "events": event_data(events), "onsets_s": [round(t, 4) for t in onsets(events)]})
    peak = max(float(np.max(np.abs(x))) for x in [*mixes.values(), *(v for layer in layers for v in layer.values())])
    scale = min(1.0, .9 / peak) if peak > 0 else 1.0
    stems = {}
    report_progress(20, "Combining the recordings and musical guides…")
    for name, mix in mixes.items():
        band = MUSIC_BAND if name != "animal" else (60.0, 20000.0)
        wav = write_wav(folder / f"{name}.wav", mix * scale, rate)
        audio, image = listening(wav, band, rate)
        stems[name] = {"audio": str(audio.relative_to(output)), "image": str(image.relative_to(output)), "band": list(band)}
    for part, layer in zip(parts, layers):
        report_progress(25 + 5 * part["index"] / len(parts), "Saving the individual recording tracks…")
        part["files"] = {}
        for name, signal in layer.items():
            wav = write_wav(folder / f"part-{part['index']}-{name}.wav", signal * scale, rate)
            (audio,) = listening(wav, MUSIC_BAND, rate, image=False)
            part["files"][name] = str(audio.relative_to(output))
    ace_result = None
    ace_name = "ace"
    if combo["ace"] and run_model:
        # The ensemble id includes the seed; each model and settings revision has
        # distinct audio and manifest files, so earlier playback stays available.
        paths = generation_paths(folder, combo["ace"]["seed"], model_variant, model)
        ace_name = paths["wav"].stem
        model_config = config | {"ace_step": config["ace_step"] | {
            "model": model["checkpoint"], "inference_steps": model["steps"], "guidance_scale": model["guidance_scale"],
        }}
        report_progress(30, "Preparing the music model…")
        ace_wav, results_path = paths["wav"], paths["results"]
        # Reuse Base/Turbo audio created before filenames included the model.
        legacy = "ace" if model_variant == "base" else "ace-turbo" if model_variant == "turbo" else None
        legacy_results = folder / f"{legacy}-results.json"
        if not ace_wav.exists() and legacy and legacy_results.exists() and (folder / f"{legacy}.wav").exists():
            old = read_json(legacy_results)
            job = old.get("jobs", {}).get(f"combo-{combo['id']}", {})
            if job.get("inference_steps") == model["steps"] and job.get("guidance_scale") == model["guidance_scale"]:
                shutil.copy2(folder / f"{legacy}.wav", ace_wav)
                write_json(results_path, old)
        if not ace_wav.exists() or not results_path.exists() or not reuse:
            job = ace_job(combo, model_config, folder) | {"output": str(ace_wav.resolve())}
            if model["engine"] == "stable-audio-3":
                run_stable(layout, model, job, paths["jobs"], results_path)
            else:
                run_ace(layout, model_config, [job], paths["jobs"], results_path, ace_root, offload)
        ace_result = read_json(results_path)["jobs"][f"combo-{combo['id']}"] | {
            "model": model_config["ace_step"]["model"], "variant": model_variant,
        }
        report_progress(86, "Preparing the generated music for playback…")
        audio, image = listening(ace_wav, MUSIC_BAND, rate)
        stems["ace"] = {"audio": str(audio.relative_to(output)), "image": str(image.relative_to(output)), "band": list(MUSIC_BAND)}
    m = config["metrics"]
    report_progress(90, "Checking the composition against the measured phrases…")
    results = {}
    for name in ("response", "ace"):
        if name in stems:
            audio = load_mono(folder / f"{ace_name if name == 'ace' else name}.wav", rate)
            results[name] = scores(audio, rate, parts, m["frame_rate_hz"], m)
            if name == "ace":
                results[name]["leakage"] = leakage(audio, mixes["guide"] * scale, rate)
    manifest = {"id": combo["id"], "title": combo["title"], "created_at_utc": now(), "config_sha256": config["config_sha256"], "render_version": RENDER_VERSION,
                "spec": combo, "duration_s": combo["duration_s"], "scale": scale, "parts": parts, "stems": stems,
                "scores": results, "ace": ace_result if model["engine"] == "ace-step" else None,
                "generation": {"model_id": model_variant, "engine": model["engine"], "label": model["label"],
                               "model_config_sha256": model_fingerprint(model), "result": ace_result} if ace_result else None,
                "interpretation": config["combination"]["interpretation"]}
    # Publish only a complete result; readers retain the Turbo manifest during a
    # Base upgrade, and its separate audio URLs continue to work afterward.
    canonical = folder / "manifest.json"
    previous = read_json(canonical) if canonical.exists() else {}
    specific = folder / f"manifest-{model_variant}.json" if ace_result else folder / "manifest-constructed.json"
    publish_manifest(specific, manifest)
    if ace_result:
        publish_manifest(paths["manifest"], manifest)
    if not previous.get("stems", {}).get("ace") or ace_result and (model_variant == registry["primary_model"] or rendered_model(previous) != registry["primary_model"]):
        publish_manifest(canonical, manifest)
    report_progress(99, "Your piece is ready. Opening playback…")
    return canonical if ace_result and model_variant == registry["primary_model"] or not ace_result and not previous.get("stems", {}).get("ace") else specific


def publish_manifest(path: Path, manifest: dict):
    """Readers see only a complete result; failed upgrades retain earlier playback."""
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".manifest-", suffix=".json", delete=False) as pending:
        json.dump(manifest, pending, indent=2)
        pending.write("\n")
    Path(pending.name).replace(path)
