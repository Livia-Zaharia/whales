"""Stable Audio runner, executed only in its isolated Python environment."""

import json
import os
from pathlib import Path
import sys
import time


def progress(percent: float, stage: str):
    print("COMPOSE_PROGRESS " + json.dumps({"percent": percent, "stage": stage}), flush=True)


def main(path: str):
    spec = json.loads(Path(path).read_text())
    if os.environ.get("HF_HOME"):
        os.environ["HF_HOME"] = str((Path(spec["root"]) / os.environ["HF_HOME"]).resolve())
    import soundfile as sf
    import torch
    from stable_audio_3 import StableAudioModel
    from stable_audio_3.model import all_models
    from huggingface_hub import hf_hub_download

    model, job = spec["model"], spec["job"]
    if model.get("revision"):
        upstream = all_models[model["checkpoint"]]
        class PinnedCheckpoint:
            def resolve(self):
                return tuple(hf_hub_download(upstream.repo_id, filename=filename, revision=model["revision"])
                             for filename in (upstream.config_path, upstream.ckpt_path))
        all_models[model["checkpoint"]] = PinnedCheckpoint()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    progress(30, f"Loading {model['label']} on {device.upper()}…")
    began = time.perf_counter()
    handler = StableAudioModel.from_pretrained(model["checkpoint"], device=device)
    load_s = time.perf_counter() - began
    guide, rate = sf.read(job["src_audio"], dtype="float32", always_2d=True)
    inputs = {"prompt": job["caption"], "duration": job["duration"], "seed": job["seed"],
              "steps": model["steps"], "cfg_scale": model["guidance_scale"],
              "init_noise_level": model.get("init_noise_level", 0.6)}
    progress(35, "Encoding the selected musical guide…")
    began = time.perf_counter()
    def on_step(state):
        progress(40 + 40 * (state["i"] + 1) / model["steps"], f"Generating music: step {state['i'] + 1} of {model['steps']}…")
    with torch.inference_mode():
        audio = handler.generate(**inputs, init_audio=(rate, torch.from_numpy(guide.T.copy())), callback=on_step)
    if device == "cuda":
        torch.cuda.synchronize()
    elapsed = time.perf_counter() - began
    target = Path(job["output"])
    target.parent.mkdir(parents=True, exist_ok=True)
    sf.write(target, audio[0].float().cpu().numpy().T, handler.model.sample_rate, subtype="FLOAT")
    result = job | {"seconds": round(elapsed, 2), "model_load_s": round(load_s, 2), "device": device,
                    "parameters": inputs, "model": model["checkpoint"], "model_revision": model.get("revision")}
    Path(spec["results"]).write_text(json.dumps({"jobs": {job["id"]: result}}, indent=2) + "\n")
    progress(85, "Preparing the generated music for playback…")


if __name__ == "__main__":
    try:
        if sys.argv[1] == "--runtime":
            import torch
            cuda = torch.cuda.is_available()
            print(json.dumps({"device": "cuda" if cuda else "cpu"}))
        else:
            main(sys.argv[1])
    except Exception as error:
        print(f"ERROR: Stable Audio: {error}", file=sys.stderr, flush=True)
        raise SystemExit(2) from error
