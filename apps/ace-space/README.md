---
title: Whale and Dolphin Orchestra · ACE-Step 1.5
emoji: 🐋
colorFrom: blue
colorTo: indigo
sdk: gradio
sdk_version: 6.2.0
python_version: 3.12
app_file: app.py
pinned: false
models:
  - ACE-Step/acestep-v15-base
  - ACE-Step/Ace-Step1.5
short_description: Compose from measured whale and dolphin phrases with ZeroGPU
---

ACE-Step 1.5 composes instrumental music from the orchestra's measured audio
guides. The generated music does not decode animal meaning.

The `/generate` API accepts an audio file and a JSON job, and returns a WAV and
generation metadata. `/health` checks readiness without allocating a GPU. Both
Base and Turbo are loaded at startup; GPU work runs only inside `spaces.GPU`.
The VAE and text encoder are shared between the two models. The language model
and training stack are not loaded.

Source and checkpoint revisions are pinned in `model-config.json`, generated
by `uv run main.py follow space deploy`. The deployment copies the pinned
ACE-Step source into this Space; the model weights download from the
[ACE-Step organization](https://huggingface.co/ACE-Step) when it starts.

Whales wakes/checks this endpoint while also rendering on local CPU. The first
successful result wins, and the other job is cancelled. If the Space is starting,
unreachable, out of quota or fails, CPU generation continues independently.
