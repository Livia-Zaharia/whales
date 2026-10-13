"""Deploy the orchestra's pinned ACE-Step Gradio app to a ZeroGPU Space."""

import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
from typing import Annotated

from huggingface_hub import HfApi, SpaceHardware
import typer

from .data import ROOT

app = typer.Typer(help="Deploy and inspect the Hugging Face ZeroGPU music backend.", no_args_is_help=True)
SOURCE = ROOT / "apps" / "ace-space"


def configure_env(space: str):
    """Update only the backend settings, retaining other sessions' local settings."""
    path = ROOT / ".env"
    text = path.read_text() if path.exists() else (ROOT / ".env.template").read_text()
    for key, value in {"ORCHESTRA_HF_SPACE": space, "ORCHESTRA_ACE_BACKEND": "auto", "ORCHESTRA_ACE": "yes"}.items():
        pattern = rf"(?m)^\s*(?:export\s+)?{key}=.*$"
        if re.search(pattern, text):
            text = re.sub(pattern, f"{key}={value}", text)
        else:
            text = text.rstrip() + f"\n{key}={value}\n"
    path.write_text(text)


def deploy_space(repo_id: str, ace_root: Path, private: bool = True):
    from orchestra import ACE_COMMIT

    api = HfApi()
    # Fail before creating anything if the pinned source is not available locally.
    archive = subprocess.run(["git", "-C", str(ace_root), "archive", ACE_COMMIT, "acestep", "LICENSE"],
                             capture_output=True, check=True).stdout
    settings = json.loads((ROOT / "resources" / "follow-music.json").read_text())["ace_step"]
    with tempfile.TemporaryDirectory() as scratch:
        folder = Path(scratch)
        shutil.copytree(SOURCE, folder, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(folder, filter="data")
        (folder / "model-config.json").write_text(json.dumps(settings | {"ace_git_revision": ACE_COMMIT}, indent=2) + "\n")
        api.create_repo(repo_id, repo_type="space", space_sdk="gradio", private=private,
                        space_hardware=SpaceHardware.ZERO_A10G, exist_ok=True)
        api.upload_folder(repo_id=repo_id, repo_type="space", folder_path=folder,
                          commit_message="Deploy pinned ACE-Step 1.5 orchestra ZeroGPU API")
    runtime = api.get_space_runtime(repo_id)
    if runtime.requested_hardware != SpaceHardware.ZERO_A10G:
        api.request_space_hardware(repo_id, SpaceHardware.ZERO_A10G)
    configure_env(repo_id)
    return f"https://huggingface.co/spaces/{repo_id}"


@app.command("deploy")
def deploy_command(
    space: Annotated[str | None, typer.Option(help="Space repo id; defaults to your-account/ace-step-whales.")] = None,
    ace_root: Annotated[Path, typer.Option(help="Local ACE-Step checkout containing the pinned commit.")] = ROOT / "data/interim/tools/ACE-Step-1.5",
    private: Annotated[bool, typer.Option(help="Create a private Space accessible using your HF token.")] = True,
):
    """Create/update the ZeroGPU Space and configure .env for HF with local CPU fallback."""
    from orchestra import load_env

    load_env()
    if space is None:
        space = HfApi().whoami()["name"] + "/ace-step-whales"
    typer.echo(deploy_space(space, ace_root, private))
    typer.echo("Configured HF first, local CPU fallback. Restart the orchestra to use the new settings.")


@app.command("status")
def status_command():
    """Check Space readiness and included GPU quota without allocating a GPU."""
    from orchestra import load_env
    from .follow_hf import space_status

    load_env()
    typer.echo(json.dumps(space_status(), indent=2))


if __name__ == "__main__":
    app()
