# ACE-Step on Hugging Face ZeroGPU

The orchestra can run its pinned ACE-Step 1.5 code and checkpoints in a private
Gradio Space. Deploy it using the account's write token in `.env`:

```bash
uv run main.py follow space deploy
uv run main.py follow space status
```

Deployment copies the pinned upstream source, Base and Turbo API, and checkpoint
revisions into `your-account/ace-step-whales`, selects ZeroGPU hardware, and
updates these local settings:

```dotenv
ORCHESTRA_HF_SPACE=your-account/ace-step-whales
ORCHESTRA_ACE_BACKEND=auto
ORCHESTRA_ACE=yes
ORCHESTRA_HF_TIMEOUT=900
```

Restart the orchestra after changing these settings. `uv run start` prepares
local CPU inference even when a Space is configured. An existing ACE-Step CUDA
environment can also run this CPU competitor; it does not need to be replaced.

For each new generation, HF and CPU write to independent scratch directories.
HF wakes the Space when needed, waits for model readiness, checks the pinned
revisions and included quota, then uploads the measured guide with the same
caption, seed, task and generation settings used locally. The first successful
complete render publishes the WAV and results JSON, and the losing process is
stopped. A losing HF request is cancelled through Gradio. HF timeout, insufficient
quota or failure leaves CPU running. Backend and winner are recorded in results
and the composition's ACE metadata. Saved complete pieces are reused as usual.

Both DiT models load at Space startup and share their VAE and text encoder. The
language model is not loaded. ZeroGPU allocates a GPU only during generation;
the readiness endpoint uses no GPU. CPU and CUDA floating-point computations
can produce different music even with identical seeds and settings.

Set `ORCHESTRA_ACE_BACKEND=cpu` to generate only on CPU, or `local` to use the
original local runner, including its CUDA device when available. The HF token
stays in the server's environment and is not returned to the browser.

Source: [Hugging Face ZeroGPU documentation](https://huggingface.co/docs/hub/spaces-zerogpu).
