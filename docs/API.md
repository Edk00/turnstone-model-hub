# Model Hub HTTP API

Base URL `http://127.0.0.1:8099`. All responses are JSON unless noted. Errors are
`{"error": "..."}` with a 4xx/5xx status.

**POST requests** must send `Content-Type: application/json` and the header `X-Model-Hub: 1`
(see SECURITY.md). Example:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8099/api/local/gpt-oss-20b/start `
  -Headers @{ "X-Model-Hub" = "1" } -ContentType application/json -Body "{}"
```

## System

| Method | Path | Returns |
|---|---|---|
| GET | `/api/info` | Version, whether a Turnstone/GitHub token is configured, hardware and estimate settings. |
| GET | `/api/system` | `cpu`, `memory` (Windows-visible + installed), `gpu` (kind, dedicated, shared limit, used dedicated/shared, `gpu_capacity_gb` fit limit, `offload_capacity_gb`, `free_now_gb`, reserves, sources), `disk`, `turnstone` (up, nodes, versions, WSL state), `servers`. |
| GET | `/api/hardware` | `unified_memory` (true when the RAM split planner applies), `kind` (integrated/discrete/none) and its source, detected adapters, primary GPU, backend devices, installed RAM, RAM bandwidth, effective bandwidth/compute with sources, and `budget`. |
| GET | `/api/hardware/split?reserved=<GB>&fraction=<0.1-1>` | Unified-memory PCs: current RAM split (BIOS GPU reservation, Windows RAM, shared limit and %), a planned split's budget, and each installed model's fit under it. Without parameters, the current split. **409** when unified memory is not detected. |
| POST | `/api/hardware/plan` | `{enabled, gpu_reserved_gb, shared_fraction}` — use a planned split for fit estimates (saved to hub.json), or `{enabled:false}` to go back to the detected split. Does not change the PC. Enabling returns **409** when unified memory is not detected. |
| POST | `/api/hardware/redetect` | Re-runs detection (e.g. after changing BIOS GPU memory or adding a GPU). |
| GET | `/api/monitor?since=<t>` | Monitor `mode`, `running`, `interval`, `duration`, `elapsed`, `remaining`, `stop_reason`, `count`, `stats` (avg/min/max/last for cpu_percent, ram_used_gb, gpu_used_gb) and samples after `since`. |
| POST | `/api/monitor` | `{mode: "continuous"\|"duration"\|"average"\|"stopped", seconds?, interval?}` — start a mode or stop. |

## Installed models

| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/api/local` | – | `models[]` (registry entry + files, size, verified, architecture, params, used_for, supports_tools, server, in_turnstone, estimate, benchmark), `loaded_gb`, `calibration`, `turnstone` (token, default_alias, error). |
| POST | `/api/local/{name}/start` | – | Starts llama-server for the model (listens on 127.0.0.1 and the WSL adapter). |
| POST | `/api/local/{name}/stop` | – | Stops the llama-server on the model's port. |
| POST | `/api/local/{name}/benchmark` | – | Runs a 256-token generation; returns measured decode/prefill tokens/s, estimate and ratio; saved to `benchmarks.json`. |
| POST | `/api/local/{name}/register` | – | Creates a Turnstone model definition (`openai-compatible`, `http://<wsl-ip>:<port>/v1`) and reloads nodes. Needs a token. |
| POST | `/api/local/{name}/reach-test` | – | `{url, from, ok, detail}`: fetches the model's `/health` from inside Turnstone's node-1 container (or from WSL if Turnstone is stopped). |
| POST | `/api/local/{name}/make-available` | – | Start → wait until healthy → reach test → register (if token and Turnstone running). |
| GET | `/api/turnstone/endpoints` | – | Each registered local model's base URL in Turnstone vs. where it listens now (`ok`). |
| POST | `/api/turnstone/repair` | – | Updates stale base URLs in Turnstone (PUT model-definitions) and reloads nodes. |

## Discovery

| Method | Path | Query | Returns |
|---|---|---|---|
| GET | `/api/discover/successors` | – | Per family: `installed`, `installed_newest`, `successors[]` or `latest[]`, each with `gguf` (best conversion, publisher tier). |
| GET | `/api/discover/popular` | – | `hf_trending`, `hf_trending_gguf`, `hf_most_downloaded_gguf`, `github_rising_llm`, `github_local_llm`. |
| GET | `/api/discover/watchlist` | – | Candidates with params, release date, licence, `gguf`, `best_fit` (variant, size, estimate), `smallest_gb`. |
| GET | `/api/search` | `q`, `source` = huggingface\|modelscope\|github, `gguf` = 1\|0 | Search results with publisher tier and `modified` flag. |
| GET | `/api/repo` | `id` = owner/name, `source` | Repo info, `used_for`, tool support, config source, `variants[]` (files, size, quant, estimate, safety, `recommended`). |
| GET | `/api/github/releases` | `repo` = owner/name | Recent releases with assets, SHA-256 digest and safety verdict. |

## Downloads

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/downloads` | – | Jobs: status (queued/downloading/verified/failed/cancelled), bytes, speed, current file, error, result (registry entry). |
| POST | `/api/downloads` | Add `make_available: true` to start, reach-test and register the model when the download finishes. Hugging Face / ModelScope: `{source, repo, variant, name?}` · GitHub: `{source:"github", repo, tag, asset, name?}` · Direct: `{source:"direct", url, sha256, name?}` | The queued job. |
| POST | `/api/downloads/{id}/cancel` | – | `{ok:true}`. Partial file is kept and resumes next time. |

## Sources

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/sources` | – | Sources and trusted publisher lists. |
| POST | `/api/sources/check` | `{url}` | `verdict` (safe/caution/unsafe), `checks[]`, `redirects[]`, size, known source. |
| POST | `/api/sources` | `{url, name}` | Adds a reviewed source if the URL is not unsafe. |
| POST | `/api/sources/{id}/trust` | `{trust}` = trusted\|reviewed\|blocked | Updated source. |

## Estimator, Turnstone, export

| Method | Path | Query | Returns |
|---|---|---|---|
| GET | `/api/compare` | `ctx` (per request), `parallel`, `prompt`, `output` (all optional) | Local estimates (incl. best speed: measured > calibrated > estimated, energy cost), cloud estimates, pricing, defaults. |
| GET | `/api/turnstone/models` | – | Turnstone model definitions and default alias (needs token). |
| GET | `/api/export.json` | `full=1` adds watchlist + successors | Snapshot. |
| GET | `/api/export.csv` | `full=1` adds watchlist rows | CSV (attachment). |
| GET | `/api/export.html` | `full=1` | Self-contained HTML report (attachment). |
