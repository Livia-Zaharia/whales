"""Configured music engines; each model keeps its own result for an ensemble."""

import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import shutil

from .data import ROOT, Layout, read_json, write_json

CONFIG = ROOT / "resources" / "orchestra-app.json"
STABLE_DIR = Path("tools") / "Stable-Audio-3"
STABLE_REVISION = "77e0398c26f7d22d801ec1bb036307e8b231f741"


def setup_stable(layout: Layout, cpu: bool = False):
    """Keep Stable Audio's pinned Torch stack separate from ACE-Step."""
    root = (layout.interim / STABLE_DIR).resolve()
    root.parent.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        subprocess.run(["git", "clone", "https://github.com/Stability-AI/stable-audio-3.git", str(root)], check=True)
    subprocess.run(["git", "checkout", STABLE_REVISION], cwd=root, check=True)
    uv = shutil.which("uv") or "uv"
    if cpu:
        if not (root / ".venv/bin/python").exists():
            subprocess.run([uv, "venv", "--python", "3.12", ".venv"], cwd=root, check=True)
        python = str(root / ".venv/bin/python")
        subprocess.run([uv, "pip", "install", "--python", python, "torch==2.7.1+cpu", "torchaudio==2.7.1+cpu", "--index-url", "https://download.pytorch.org/whl/cpu"], cwd=root, check=True)
        subprocess.run([uv, "pip", "install", "--python", python, ".", "huggingface-hub==1.7.1", "transformers==5.8.0",
                        "numpy==2.4.3", "soundfile==0.13.1", "safetensors==0.7.0"], cwd=root, check=True)
    else:
        subprocess.run([uv, "sync", "--python", "3.12", "--no-dev", "--frozen"], cwd=root, check=True)
    return root


def model_registry(path: Path = CONFIG) -> dict:
    registry = read_json(path)["music"]
    models = registry.get("models", [])
    ids = [m.get("id", "") for m in models]
    if not models or len(set(ids)) != len(ids) or any(not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", i) for i in ids):
        raise ValueError("Music model ids must be unique lowercase names")
    if registry.get("default_model") not in ids or registry.get("primary_model") not in ids:
        raise ValueError("Default and primary music models must be configured")
    profiles = registry.get("caption_profiles", {})
    if not isinstance(profiles, dict) or any(not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name)
                                              or not isinstance(profile, str) or profile not in {"legacy", "instruments"}
                                              for name, profile in profiles.items()):
        raise ValueError("Caption profiles must select legacy or instruments for valid model ids")
    for model in models:
        if model.get("engine") not in {"ace-step", "stable-audio-3"} or not model.get("checkpoint"):
            raise ValueError(f"Unsupported music engine for {model['id']}")
        if not isinstance(model.get("steps"), int) or model["steps"] < 1:
            raise ValueError(f"Invalid step count for {model['id']}")
        if not isinstance(model.get("guidance_scale"), (int, float)) or not math.isfinite(model["guidance_scale"]) or model["guidance_scale"] < 0:
            raise ValueError(f"Invalid guidance for {model['id']}")
    return registry


def music_model(model_id: str, registry: dict | None = None) -> dict:
    registry = registry or model_registry()
    for model in registry["models"]:
        if model["id"] == model_id:
            return model
    raise ValueError(f"Unknown music model: {model_id}")


def model_fingerprint(model: dict) -> str:
    # Display copy can change without discarding generated audio.
    settings = {k: int(v) if isinstance(v, float) and v.is_integer() else v for k, v in model.items()
                if k not in {"label", "description", "recommended", "characteristics", "available", "fingerprint", "device", "reuse_primary"}}
    return hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def generation_paths(folder: Path, seed: int, model_id: str, model: dict) -> dict[str, Path]:
    stem = f"generated-{seed}-{model_id}-{model_fingerprint(model)[:10]}"
    return {"wav": folder / f"{stem}.wav", "jobs": folder / f"{stem}-jobs.json",
            "results": folder / f"{stem}-results.json", "manifest": folder / f"{stem}.json"}


def rendered_model(manifest: dict) -> str:
    return (manifest.get("generation") or {}).get("model_id") or (manifest.get("ace") or {}).get("variant", "base")


def run_stable(layout: Layout, model: dict, job: dict, jobs_path: Path, results_path: Path):
    root = (layout.interim / STABLE_DIR).resolve()
    python = root / ".venv" / "bin" / "python"
    if not python.exists():
        raise ValueError("Stable Audio is not installed on this server. Set up its isolated environment before composing.")
    write_json(jobs_path, {"root": str(ROOT), "model": model, "job": job, "results": str(results_path.resolve())})
    subprocess.run([str(python), str(Path(__file__).with_name("follow_stable.py")), str(jobs_path.resolve())], cwd=root, check=True)
    return read_json(results_path)
