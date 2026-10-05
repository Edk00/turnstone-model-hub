# Introducing Turnstone Model Hub

*Draft introduction for a GitHub release, a GitHub Discussions post, or the Turnstone community Discord.
Edit freely before posting.*

---

## Model Hub: a companion for running Turnstone on your own models

> **Standalone and unofficial.** Model Hub is an independent community project. It is not created,
> maintained or endorsed by [Turnstone Labs](https://github.com/turnstonelabs), and is not part of or
> affiliated with [Turnstone](https://github.com/turnstonelabs/turnstone).

Turnstone is built around a simple promise: your agents, your models, your hardware. The "your
models" part is where most of us spend the most time — which model to run, which quantization, will
it fit, how fast will it be, and how to wire it into the console.

**Turnstone Model Hub** is a small, standalone, Apache-2.0 dashboard that handles that side. It runs
on the machine that hosts your models and talks to Turnstone only through the console's admin API.

### What you get

- **A live view of your hardware** — GPU type and memory, live usage, RAM bandwidth and the memory
  actually left for models, all auto-detected.
- **Unified-memory planning** — on unified-memory APUs (e.g. AMD Ryzen AI Max), see how the BIOS splits the
  shared RAM between CPU and GPU and what another split would let you run. (Greyed out on GPUs with
  their own VRAM, where it doesn't apply.) On unified memory, RAM figures are indicative and may not
  be correct, because Windows counts the shared pool in overlapping buckets; the dashboard says so
  where they appear.
- **Fit and speed before you download** — every GGUF variant in a repo with memory needed for your
  context, a fit verdict, estimated tokens/s and seconds per Turnstone agent turn. Benchmark once and
  the estimates calibrate to your machine.
- **What to run next** — newer releases in the families you already use, a watchlist (DeepSeek, Kimi,
  GLM, MiniMax, Qwen, Gemma, Nemotron, gpt-oss…), and what's trending on Hugging Face and GitHub.
- **Safe downloads** — weights-only formats, allow-listed HTTPS hosts, SHA-256 verified, with a
  checker for new sources.
- **One-click hand-off to Turnstone** — start the llama.cpp server, confirm a Turnstone node can reach
  it, register it in the Models tab, and fix the endpoints if the WSL address changes after a reboot.

### What it isn't

- Not part of Turnstone, and not created, maintained or endorsed by Turnstone Labs — an independent
  companion that uses Turnstone's public API.
- Not a model server — it drives [llama.cpp](https://github.com/ggml-org/llama.cpp)'s `llama-server`.
- Not a cloud service — nothing leaves your machine except requests to the model registries you
  search.

### Try it

```powershell
cd model-hub
.\run-hub.ps1
```

Python 3.11+ (standard library only), Windows with Turnstone in WSL 2. Full docs in the
[README](../README.md).

### Try it and tell us how it went

Anyone can try Model Hub and send feedback; please
[open an issue](https://github.com/Edk00/turnstone-model-hub/issues) with what worked, what didn't,
and your GPU, RAM, llama.cpp backend and Turnstone version.

So far it has been tested on one unified-memory mini PC (AMD Ryzen AI Max+ 395, 128 GB). Reports from
discrete NVIDIA/AMD GPUs, other APUs and multi-node Turnstone setups would be especially useful, as
would thoughts on the longer-term idea in [INTEGRATION.md](INTEGRATION.md): Model Hub as a per-host
agent behind a native "Models" view in the Turnstone console.

### Credits

Turnstone, its console API and its installer are the work of
[Turnstone Labs](https://github.com/turnstonelabs). Model Hub builds on their public API and on
[llama.cpp](https://github.com/ggml-org/llama.cpp) (ggml-org).

---

### Short version (for a chat post)

> Built a small companion for Turnstone: **Model Hub** — a local dashboard that shows what your
> hardware can run (incl. a unified-memory planner for APUs), estimates fit and
> tokens/s per GGUF before you download, tracks new releases (DeepSeek, Kimi, GLM, Qwen, Gemma…),
> downloads with SHA-256 checks, and registers the model in Turnstone's console in one step.
> Apache-2.0, stdlib-only Python. Standalone and unofficial: not created, maintained or endorsed by
> Turnstone Labs (makers of Turnstone, https://github.com/turnstonelabs/turnstone). Try it and send feedback:
> https://github.com/Edk00/turnstone-model-hub/issues
