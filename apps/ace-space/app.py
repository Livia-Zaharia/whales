"""Small, pinned ACE-Step API for the orchestra's ZeroGPU backend."""

# Import spaces before torch/ACE-Step so it can intercept CUDA initialization.
import spaces

import copy
import json
import math
from pathlib import Path
import time
import uuid

import gradio as gr
from huggingface_hub import snapshot_download
import torch

from acestep.handler import AceStepHandler
from acestep.inference import GenerationConfig, GenerationParams, generate_music
from acestep.llm_inference import LLMHandler
from acestep.model_downloader import MAIN_MODEL_COMPONENTS

ROOT = Path(__file__).resolve().parent
SETTINGS = json.loads((ROOT / "model-config.json").read_text())
CHECKPOINTS = ROOT / "checkpoints"
OUTPUT = ROOT / "generated"
OUTPUT.mkdir(exist_ok=True)
MAIN_MODEL_COMPONENTS[:] = ["vae", "Qwen3-Embedding-0.6B"]

snapshot_download(SETTINGS["main_repo"], revision=SETTINGS["main_revision"],
                  local_dir=CHECKPOINTS,
                  allow_patterns=["vae/*", "Qwen3-Embedding-0.6B/*", "acestep-v15-turbo/*"])
snapshot_download(SETTINGS["model_repo"], revision=SETTINGS["model_revision"],
                  local_dir=CHECKPOINTS / SETTINGS["model"])

# CUDA placements happen at startup, as required by ZeroGPU's emulation mode.
base = AceStepHandler()
status, ready = base.initialize_service(
    project_root=str(ROOT), config_path=SETTINGS["model"], device="cuda",
    compile_model=False, offload_to_cpu=False, offload_dit_to_cpu=False, quantization=None,
)
if not ready:
    raise RuntimeError(f"ACE-Step Base initialization failed: {status}")

# The pinned upstream handler does not yet expose shared-component arguments.
# Copy the initialized handler and load only Turbo's DiT; VAE/encoder stay shared.
turbo = copy.copy(base)
turbo._sync_model_code_if_needed("acestep-v15-turbo", CHECKPOINTS)
turbo._load_main_model_from_checkpoint(
    model_checkpoint_path=str(CHECKPOINTS / "acestep-v15-turbo"), device="cuda",
    use_flash_attention=False, compile_model=False, quantization=None,
)
HANDLERS = {SETTINGS["model"]: base, "acestep-v15-turbo": turbo}
llm = LLMHandler()  # DiT-only generation, matching the local phrase runner.


def health():
    return {"ready": True, "models": list(HANDLERS), "backend": "hf-zerogpu",
            "ace_git_revision": SETTINGS["ace_git_revision"],
            "main_revision": SETTINGS["main_revision"],
            "model_revisions": {SETTINGS["model"]: SETTINGS["model_revision"],
                                "acestep-v15-turbo": SETTINGS["main_revision"]}}


def validate_job(job_json):
    job = json.loads(job_json)
    if job.get("model") not in HANDLERS:
        raise gr.Error("Choose an available ACE-Step model")
    if job.get("task") not in {"cover", "lego", "text2music"}:
        raise gr.Error("Choose cover, lego or text2music")
    duration = float(job["duration"])
    if not math.isfinite(duration) or not 1 <= duration <= 240:
        raise gr.Error("Duration must be between 1 and 240 seconds")
    if not 1 <= int(job["inference_steps"]) <= 100:
        raise gr.Error("Inference steps must be between 1 and 100")
    if job["model"] == "acestep-v15-turbo" and job["task"] == "lego":
        raise gr.Error("Choose Base for adding a track")
    return job


def gpu_duration(source_audio, job_json, progress=None):
    job = validate_job(job_json)
    return min(180, max(30, int(20 + float(job["duration"]) * int(job["inference_steps"]) / 25)))


@spaces.GPU(duration=gpu_duration)
def generate(source_audio, job_json, progress=gr.Progress()):
    job = validate_job(job_json)
    if job["task"] != "text2music" and not source_audio:
        raise gr.Error("This task needs an audio guide")
    handler = HANDLERS[job["model"]]
    instruction = handler.generate_instruction(job["task"], track_name=job.get("track"))
    params = GenerationParams(
        task_type=job["task"], instruction=instruction, src_audio=source_audio,
        caption=job["caption"], lyrics="[Instrumental]", instrumental=True,
        duration=job["duration"], seed=job["seed"], inference_steps=job["inference_steps"],
        guidance_scale=job["guidance_scale"], thinking=False, use_cot_metas=False,
        use_cot_caption=False, use_cot_language=False, repainting_start=0.0,
        repainting_end=-1, audio_cover_strength=job["audio_cover_strength"],
    )
    config = GenerationConfig(batch_size=1, use_random_seed=False,
                              seeds=[job["seed"]], audio_format="wav")
    folder = OUTPUT / uuid.uuid4().hex
    started = time.perf_counter()
    result = generate_music(handler, llm, params, config, save_dir=str(folder), progress=progress)
    if not result.success or not result.audios:
        raise gr.Error(result.error or result.status_message or "Music generation failed")
    audio = result.audios[0]
    metadata = {"instruction": instruction, "seconds": round(time.perf_counter() - started, 2),
                "seed_used": audio["params"].get("seed"), "status": result.status_message,
                "model": job["model"], "backend": "hf-zerogpu", "device_type": "cuda",
                "device": torch.cuda.get_device_name(0), "ace_git_revision": SETTINGS["ace_git_revision"],
                "main_revision": SETTINGS["main_revision"],
                "model_revision": SETTINGS["main_revision"] if job["model"] == "acestep-v15-turbo" else SETTINGS["model_revision"],
                "torch": torch.__version__}
    return audio["path"], metadata


with gr.Blocks(title="Whale and Dolphin Orchestra · ACE-Step 1.5",
               delete_cache=(3600, 3600)) as demo:
    gr.Markdown("# ACE-Step 1.5\nCompose instrumental music from measured whale and dolphin phrases.")
    source = gr.Audio(type="filepath", label="Measured audio guide")
    job = gr.Textbox(label="Generation job (JSON)", lines=8)
    button = gr.Button("Compose", variant="primary")
    audio = gr.Audio(type="filepath", label="Generated music")
    metadata = gr.JSON(label="Generation details")
    button.click(generate, [source, job], [audio, metadata], api_name="generate",
                 concurrency_limit=1, concurrency_id="ace")
    check = gr.Button("Check readiness")
    check.click(health, [], gr.JSON(label="Model readiness"), api_name="health", queue=False)

demo.queue(max_size=4).launch()
