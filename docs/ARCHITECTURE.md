# How Model Hub works

## Big picture

```
 Browser (web/)                    Model Hub (hub/, Python, 127.0.0.1:8099)               Outside world
 ┌──────────────────┐  JSON/HTTP   ┌─────────────────────────────────────────┐
 │ index.html       │ ───────────▶ │ server.py   routes, security checks      │
 │ app.js  (tabs)   │ ◀─────────── │ system.py   CPU/RAM/GPU/disk/Turnstone   │──▶ llama-server --list-devices, netstat, wsl.exe
 │ style.css        │              │ local_models.py  registry, start/stop,   │──▶ llama-server.exe (ports 8001+), /completion
 └──────────────────┘              │                   benchmark, register    │──▶ Turnstone console admin API (8090)
                                   │ discovery.py successors/popular/watch    │──▶ Hugging Face API, GitHub API, ModelScope API
                                   │ hf.py / github.py / modelscope.py        │
                                   │ gguf.py     GGUF header reader           │──▶ local GGUF files / HTTP range requests
                                   │ estimates.py memory + speed model        │
                                   │ sources.py  safety checks                │
                                   │ downloads.py queue, resume, SHA-256      │──▶ model files → ../llm/models
                                   │ export.py   JSON / CSV / HTML            │
                                   └─────────────────────────────────────────┘
                                     config/*.json      ../llm/models.json (shared registry)
```

Turnstone itself runs in WSL/Docker and never sees Model Hub directly. The hub runs on Windows because
that's where the GPU, the llama.cpp servers and the model files live. It reaches Turnstone through
the console's published port (`127.0.0.1:8090`) and the Turnstone containers reach the model servers
through the Windows WSL adapter address (`<vEthernet (WSL) IPv4>:8001`, typically `172.x.x.1`).

## The shared model registry

`../llm/models.json` is the single list of local models. Each entry has a `name` (also the Turnstone
alias), `port`, `file` (first part for split GGUFs), the Hugging Face `repo` it came from, its
`base_model`, a `family` (matching `config/watchlist.json`), and `ctx`/`parallel` for llama-server.
`llm-start.ps1`/`llm-stop.ps1` and the hub both read it, and the hub appends to it after a download
completes. Ports are assigned from 8001 upward.

## Request flow per tab

| Tab | Endpoint | What happens |
|---|---|---|
| Resource cards | `GET /api/system` (each monitoring interval; never while monitoring is Stopped) | `system.snapshot()`. `hardware.py` detects the GPU once (DXGI via `dxgi.py`, RAM modules via WMI, backend devices via `llama-server --list-devices`); per request it reads Windows GPU counters (`pdh.py`) and RAM, and computes the model memory budget. Listening ports come from one `netstat` call (Windows takes ~2 s per refused connection, so closed ports are never probed). |
| Hardware detection panel | `GET /api/hardware`, `GET /api/hardware/split`, `POST /api/hardware/plan` | Shows every detected value with its source. When `unified_memory` is true it also shows a note that RAM figures are indicative (see ASSUMPTIONS.md). The RAM split planner is rendered only when `unified_memory` is true; otherwise the panel is greyed out with disabled controls and the split/plan endpoints refuse (409). |
| Monitoring bar | `GET/POST /api/monitor` | `monitor.py` samples CPU/RAM/GPU on its own thread only while a mode is active (continuous, set duration, average over duration); stats are avg/min/max. |
| Installed | `GET /api/local` | For each registry entry: file presence + `.verified` marker, GGUF header (architecture, tool support from the chat template), Hugging Face info for the base model (params, task, tags), architecture config (base `config.json`, else the GGUF header), an estimate, server status, benchmark, Turnstone registration (if a token is set). |
| Newer versions | `GET /api/discover/successors` | For every family in the watchlist: list the publisher's models by creation date, keep names matching the family regex and not matching `exclude`, keep those newer than your newest installed model of that family, and find the best GGUF conversion for each. |
| Popular | `GET /api/discover/popular` | HF trending/most-downloaded (GGUF filter) and GitHub search (rising: created in 90 days with >300 stars; established: `topic:local-llm` >1000 stars). Non-LLM tasks (image generation etc.) are filtered out. |
| Watchlist | `GET /api/discover/watchlist` | For each candidate base model: HF info, best GGUF repo, every variant's estimate, and the recommended variant (see ASSUMPTIONS.md). |
| Search | `GET /api/search`, `GET /api/repo` | Search a source; open a repo to group its GGUF files into variants (split parts merged, vision projectors and draft/MTP heads listed separately), estimate each, mark the suggested one. |
| Download | `POST /api/downloads` | Resolves files, checks type/host/checksum rules, queues a job. The worker resumes `.part` files with HTTP Range, refuses redirects to non-allow-listed hosts, verifies SHA-256 and GGUF magic, renames, writes `.verified`, then adds the model to `models.json`. |
| Estimator | `GET /api/compare` | Re-estimates installed models with your context/parallel/turn sizes; uses measured speed where benchmarked, else the calibrated estimate; adds cloud rows from `pricing.json`. |
| Sources | `GET/POST /api/sources…` | Lists sources; runs the URL safety checks; adds a reviewed source; changes trust. |
| Export | `GET /api/export.{json,csv,html}` | Snapshot of the above. |

## Caching

`net.get_json` caches GET responses in memory: Hugging Face/ModelScope 30 min, GitHub 60 min
(configurable in `hub.json`). Restart the hub or wait for expiry to force fresh data. GGUF headers of
local files are cached by path + modification time.

## Calibration loop

The Benchmark button runs a 256-token generation on the running server and stores the measured
speeds in `../llm/benchmarks.json` along with the ratio measured/estimated. The median ratio across
benchmarks becomes a calibration factor applied to the speed estimates of models you haven't
benchmarked (shown as "calibrated est.").

## Concurrency

`ThreadingHTTPServer` handles requests in threads. Discovery fans out with an 8-thread pool.
Downloads run on `max_parallel` worker threads (default 1, so a large model gets the full
bandwidth). Registry writes go through a lock and an atomic replace.
