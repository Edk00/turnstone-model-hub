# Notes and assumptions

Every number on the dashboard is either **measured** (labelled) or **estimated** with the rules
below. Estimates are meant for comparing models and spotting what won't fit — expect ±30% on speed
until you benchmark. All tunables are in `config/hub.json`.

## Hardware detection

Nothing about the hardware is typed in: every value is detected at startup (or with **Re-detect** in
the "Hardware detection" panel) unless you override it in `config/hub.json` (`null` = auto). The panel
shows the source of each value.

| Item | How it is obtained |
|---|---|
| GPU name, vendor, dedicated memory, shared-memory limit | **DXGI** (`IDXGIAdapter1::GetDesc1`), vendor-neutral (AMD/NVIDIA/Intel); software adapters skipped. Fallback: display-adapter registry key (shared limit then assumed 50% of RAM). |
| Unified memory (integrated) vs dedicated VRAM (discrete) | Integrated if installed RAM − Windows-visible RAM ≈ dedicated GPU memory (a firmware carve-out), or Intel iGPU, or AMD "Radeon(TM) … Graphics" APU naming; otherwise discrete. `gpu_kind_override` forces the result. Decides whether the RAM split planner is active. |
| GPU memory in use (system-wide) | Windows "GPU Adapter Memory" performance counters (Task Manager's source), matched to the adapter by LUID. |
| GPU memory the inference backend sees | `llama-server --list-devices` (informational; on unified memory this is roughly dedicated + shared limit). |
| CPU, threads, utilisation | Windows registry; `GetSystemTimes` deltas. |
| RAM installed / visible / available | `GetPhysicallyInstalledSystemMemory`, `GlobalMemoryStatusEx`. |
| Peak RAM bandwidth | WMI `Win32_PhysicalMemory`: speed × bus width. Soldered LPDDR: module widths summed (e.g. 8 × 32-bit × 8000 MT/s = 256 GB/s); socketed DIMM/SODIMM: assumed dual-channel (128-bit) regardless of stick count (e.g. DDR5-6000 → 96 GB/s). |
| Effective bandwidth for speed estimates | Integrated GPU / CPU: peak RAM bandwidth × `bandwidth_efficiency` (0.45). Discrete GPU: `discrete_vram_bandwidth_gbps` × 0.45 if set, else **assumed** 400 GB/s. Benchmarks then calibrate. |
| Effective compute for prompt speed | `effective_tflops` (8), **assumed**, not detectable; benchmarks calibrate generation speed only. |
| Turnstone node count | Console `/health` when Turnstone is running, else `turnstone.nodes` in hub.json. |

Values are shown in GB as Windows shows them (binary, 1 GB = 1024³ bytes) for memory; download sizes
use decimal GB as Hugging Face does (so a "12.1 GB" file occupies 11.3 GB of memory).

## RAM segregation on unified-memory PCs

On a unified-memory machine (for example AMD Ryzen AI Max / Strix Halo APUs, other APUs, Intel iGPUs)
there is **one physical pool of RAM** and no separate VRAM: the CPU and the GPU read the same chips at
the same speed. What *is* separate is how firmware and Windows hand that pool out:

1. **BIOS reservation ("UMA Frame Buffer Size", a.k.a. "dedicated" GPU memory).** The BIOS sets
   aside a fixed block for the GPU before Windows starts. Windows can never use it, and Task Manager
   shows it as "hardware reserved". Many vendors ship large-memory APUs with half the RAM reserved;
   the BIOS usually allows anything from about 0.5–1 GB up to roughly three quarters of the RAM.
2. **Windows RAM.** Everything not reserved. Windows, apps, WSL and the Turnstone containers live here.
3. **Shared (borrowed) GPU memory.** Windows lets the GPU borrow part of its RAM on demand. The limit is
   set by Windows and the GPU driver, usually 50% of Windows RAM, sometimes more (the hub reads the
   actual value). Borrowed memory is as fast as reserved memory on unified memory, but it is taken
   from Windows.

**When this applies.** The RAM split planner, the "Use this split for fit estimates" option and
planned-split fit estimates are **only active when unified memory is detected** (GPU type
`integrated`). On a PC whose GPU has its own VRAM (`discrete`) or with no GPU (`none`), the
"Hardware detection" panel is greyed out, the planner's controls are disabled, a saved plan is
ignored, and `/api/hardware/split` and `/api/hardware/plan` return 409. The other detected values stay
visible because fit estimates still use them. If detection is wrong, set
`hardware.gpu_kind_override` in `config/hub.json` to `"integrated"`, `"discrete"` or `"none"` and
press Re-detect.

### Disclaimer: RAM figures on unified-memory PCs are indicative

> **When unified memory is detected, every RAM and GPU-memory figure Model Hub shows is indicative
> only and may not be correct.** The dashboard repeats this as a note in the Hardware detection panel.

Why:

- **One pool, several overlapping counters.** Windows reports the BIOS reservation, Windows RAM and GPU
  "shared" memory separately. GPU shared memory is Windows RAM, so the same bytes can show up as
  "used" in both the Memory card and the GPU card, or in neither, depending on how the driver
  allocates it.
- **Driver behaviour isn't fully visible.** The AMD driver, Vulkan/ROCm and llama.cpp can map
  host-visible memory, pin pages or allocate lazily in ways the Windows counters (DXGI, "GPU Adapter
  Memory") don't reflect, or reflect late.
- **The shared limit is reported, not guaranteed.** The shared limit shown is what DXGI reports. The
  amount the driver actually grants can differ, and can change after a driver, BIOS or Windows update
  (for example AMD Variable Graphics Memory).
- **The installed − visible ≈ reserved rule is approximate.** Firmware also keeps a little memory for
  itself, so the detected BIOS reservation can be off by up to about a gigabyte.
- **Planned splits are extrapolations.** The planner assumes the driver behaves after a BIOS change as
  it does now.

What to do: treat "fit" and "free now" as guides. Compare with Task Manager (Performance → Memory and
GPU), and confirm with a real load and **Benchmark** before relying on a model fitting. Keep
headroom (`gpu_reserve_gb`, `windows_reserve_gb`) rather than planning to the last gigabyte. On
discrete GPUs (dedicated VRAM) the figures are far more reliable, because VRAM and system RAM are
counted separately.

So the GPU can use up to *BIOS reservation + shared limit*, and the CPU side has the Windows RAM, part
of which may be lent to the GPU. The split is **adjustable** only in the BIOS; Model Hub cannot and does
not change it. The dashboard's **RAM split planner** (Hardware detection panel) shows what another
split would give, and "Use this split for fit estimates" makes Fit/Estimator assume it (saved as
`hardware.plan` in hub.json) until you apply it in the BIOS and press Re-detect.

Illustrative example: a hypothetical 128 GB unified-memory PC, with the defaults of an 8 GB Windows
reserve, 8 GB for Turnstone (1 node) and 2 GB GPU headroom:

| BIOS reservation | Shared % | Windows RAM | GPU memory for models | Windows left at full model load |
|---|---|---|---|---|
| 0.5 GB | 50% (typical Windows) | 127.5 GB | 63.8 GB | 63.8 GB |
| 0.5 GB | 75% (if the driver allows it) | 127.5 GB | 95.6 GB | 31.9 GB |
| 32 GB | 50% | 96 GB | 78 GB | 48 GB |
| 64 GB | 50% | 64 GB | 94 GB | 32 GB |
| 96 GB | 50% | 32 GB | 110 GB | 16 GB |

Takeaways: on **Windows**, a near-zero reservation only helps the GPU if the driver lets it borrow a
large share; with the usual 50% cap a larger reservation gives the GPU more. A large reservation
maximises GPU memory but squeezes Windows/WSL/Turnstone. The shared % after a BIOS change is not known
in advance, so re-detect after changing it. (On Linux the equivalent is the amdgpu GTT size, which can
be raised with a small BIOS reservation; that is outside this Windows setup.)

## Memory budget for models

```
integrated GPU (unified memory):
  fit limit = (dedicated − GPU reserve) + min(shared limit, RAM − Windows reserve − Turnstone stack)
  free now  = (dedicated − dedicated in use − 2) + min(shared limit − shared in use, available RAM − 4)
discrete GPU:
  GPU limit = VRAM − GPU reserve;  beyond that, layers can run on the CPU from
  RAM − Windows reserve − Turnstone stack ("partial CPU offload", much slower)
  e.g. 16 GB card, 64 GB RAM: GPU limit ≈ 15.9 − 2 ≈ 14 GB; with offload ≈ 14 + (63.2 − 8 − 8) ≈ 61 GB
no GPU:     CPU only, from RAM − Windows reserve − Turnstone stack
```

- Shared GPU memory on an integrated GPU is ordinary RAM, so it competes with Windows, WSL and the
  Turnstone containers. `windows_reserve_gb` (8) keeps room for Windows and apps; the Turnstone stack
  is reserved at nodes × 4 GB (each node's Docker limit) + 4 GB (postgres, console, caddy, searxng),
  i.e. 8 GB with 1 node, 24 GB with 5, 44 GB with 10 (10 nodes also needs WSL's memory limit raised).
- On a discrete GPU, Windows' "shared GPU memory" is not used for model weights (it is slow over PCIe);
  llama.cpp keeps the overflow on the CPU instead, which is what the offload category models.
- `gpu_reserve_gb` (2) is kept free for the desktop and driver.

## Memory needed to run a model

```
memory = weights + KV cache + runtime overhead
weights  = file size (all split parts)
KV cache = bytes_per_token × context_per_request × parallel_slots
overhead = 1.5 GB (runtime_overhead_gb: compute buffers, graph, scratch)
```

- **bytes_per_token** = full-attention layers × 2 (K and V) × KV heads × head dim × 2 bytes (f16 cache,
  `kv_bytes_per_element`). Architecture values come from the base model's `config.json`; if it is
  gated or missing, from the GGUF file header (local file, or the first ~24 MB of the remote file via an
  HTTP range request).
- **Sliding-window layers** (Gemma 2/3/4, gpt-oss) only cache `window` tokens. Gemma 3's pattern is
  assumed to be 5 local : 1 global, gpt-oss/Gemma 2 alternate; Gemma 4 GGUFs carry the pattern.
- **Hybrid models** (Qwen 3.5+/3.6 linear attention, Nemotron-H Mamba layers, Kimi Linear) only count
  their attention layers; recurrent state is assumed small and covered by the overhead.
- **MLA models** (DeepSeek V3+, Kimi K2) use the compressed cache: layers × (kv_lora_rank + rope dim) × 2 bytes.
- If nothing is known: KV = 10% of weights per 32k tokens of total context.
- Defaults: 65,536 tokens per request × 2 parallel slots (`default_ctx`, `default_parallel`) for
  candidates; installed models use their `ctx`/`parallel` from `models.json`.
- Not modelled: quantized KV cache (`-ctk q8_0` would roughly halve KV), flash-attention savings,
  vision projector memory (~0.6–1.2 GB when loaded), CPU offload.

## Fit

| Label | Rule |
|---|---|
| fits (GPU) | memory ≤ dedicated − GPU reserve. Full GPU speed. |
| fits (borrowed RAM) | integrated GPU: needs more than the BIOS GPU reservation but ≤ fit limit. Same speed (unified memory), but the extra comes out of Windows RAM, so Windows/WSL/Turnstone have less while it runs. |
| partial CPU offload | discrete GPU: memory ≤ VRAM + RAM budget. Layers beyond VRAM run on the CPU; speed estimated as a bandwidth-weighted mix. |
| CPU only | no GPU: memory ≤ RAM budget. |
| does not fit | over every limit. |

Under the label: **room right now** = it would load with the memory currently free (GPU counters +
available RAM); **free memory first** = it fits the machine but other models or apps must be stopped.

## Speed

```
active parameters  = from the name "…-A3B" (3B), else MoE config
                     (total − expert params × (1 − experts_used/experts)), else total (dense)
bits per weight    = file size × 8 / total parameters
generation tok/s   ≈ effective_bandwidth / (active parameters × bits per weight / 8)
prompt tok/s       ≈ effective_tflops / (2 × active parameters)
```

- Generation is memory-bandwidth-bound: each token reads the active weights once. This
  underestimates MoE speed when hot experts stay cached and overestimates it at long context (KV reads
  grow with context).
- **Calibration:** Benchmark runs a 256-token generation and stores measured ÷ estimated. The median
  ratio is applied to all unbenchmarked models ("calibrated est."). The 45% bandwidth efficiency
  default is deliberately conservative, so measured speeds are often higher than the first estimate.
- Measured prompt speed from the 20-token benchmark prompt is not representative; the estimator uses
  the higher of measured and estimated prompt speed.

## Turnstone usage estimate

An **agent turn** is one model call in a Turnstone workstream: the prompt (system prompt, tool
definitions, conversation so far, tool results) plus the generated reply or tool call.

```
turn seconds = prompt tokens / prompt tok/s + output tokens / generation tok/s
turns/hour   = 3600 / turn seconds × parallel slots
```

- Defaults: 6,000 prompt + 800 output tokens (`agent_turn_prompt_tokens`, `agent_turn_output_tokens`),
  adjustable in the Estimator. Real turns grow as a workstream gets longer.
- llama.cpp's prompt cache reuses the unchanged prefix of a conversation, so later turns usually
  process far fewer than the full prompt tokens; the estimate ignores this (pessimistic).
- Turnstone's LLM judge (enabled by default, threshold 0.95) makes an extra, shorter call per tool use,
  on the judge model (by default the same as the coordinator model). Not included.
- Parallel slots share the context: each request gets `ctx / parallel` tokens.
- **Cost:** local models cost electricity only — `local_power_watts` (default 140 W under load) ×
  turn time × `electricity_per_kwh` (unset by default, so not shown). Cloud costs use
  `config/pricing.json` prices you enter; none are pre-filled because prices change.
- Turnstone nodes: each Docker node is capped at 4 GB (`node_memory_limit_gb`); with 1 node the stack
  uses up to ~8 GB of the Windows/WSL RAM that shared GPU memory also draws from.

## Discovery

- **Newer versions:** a model is a successor if the same publisher (family `author`) released it after
  your newest installed model of that family, its name matches the family regex, and it isn't an
  excluded variant (base models, FP8/NVFP4/AWQ/GPTQ, MLX/ONNX, vision-only, audio, embeddings,
  guard/reward models, research checkpoints…). It may be a different size, not a strict upgrade.
- **Best GGUF:** the most-downloaded repo declaring the model as its quantization base, preferring
  official and known quantizers. If the successor repo itself is GGUF, it is used directly.
- **Suggested variant:** the largest 3.5–6.6 bits/weight file that fits in dedicated GPU memory;
  otherwise the 3.5–6.6-bit file closest to 4.6 bits that fits using shared memory (then partial
  offload, then CPU only); otherwise the largest lower-bit file that still leaves 10% headroom.

## Resource monitoring

| Mode | Behaviour |
|---|---|
| Continuous | Samples CPU %, RAM used and GPU memory used every *interval* seconds until you press Stop. |
| For a set duration | Same, then stops by itself after the duration (5 s – 24 h). |
| Average over a set duration | Samples for the duration, then stops and shows average, minimum and peak (the cards keep the result). |
| Stopped | No sampling and no dashboard polling; the hub does no background work. Refresh gives a one-off reading. |

Each sample costs about a millisecond (Windows counters, no subprocesses). Up to 7,200 samples are kept
in memory; the choice is remembered in the browser. Hardware detection itself runs once at startup.
- **Popular:** Hugging Face `trendingScore` and 30-day downloads; GitHub stars (rising = created in
  the last 90 days with >300 stars). Popularity is not a quality signal.
- **Used for** is derived from the Hugging Face task, tags, name keywords, a vision projector in the
  repo, and whether the GGUF chat template references `tools` (tool calling support).
- Parameters come from Hugging Face's safetensors/GGUF metadata (`total`), which counts every weight
  (embeddings included) — e.g. "36B" for Qwen3.6-35B.

## Data freshness and limits

- Hugging Face/ModelScope responses are cached 30 minutes, GitHub 60 minutes; system data refreshes
  every 10 s (GPU probe every 15 s).
- Anonymous GitHub API: 60 requests/hour and 10 searches/minute. Set `GITHUB_TOKEN` to raise limits.
- Gated Hugging Face repos (e.g. some Google/Meta originals) can't be read anonymously; the hub falls
  back to GGUF metadata from a public conversion.
- Turnstone status is read from `http://127.0.0.1:8090/health`; registration requires the console's
  admin API and a token with `approve` scope.
- Model files are saved to `llm/models` on Windows. Turnstone never reads model files; its containers
  call each model's llama-server at `http://<Windows vEthernet (WSL) address>:<port>/v1`. Servers
  listen on 127.0.0.1 and that address only. **Test reach** checks the path from inside a Turnstone
  node (or from WSL when Turnstone is stopped). If Windows assigns a new WSL address after a reboot,
  the Installed tab flags the registered models as stale and **Fix in Turnstone** updates their base URLs.
