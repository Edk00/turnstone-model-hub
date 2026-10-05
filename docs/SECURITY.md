# Security

Model Hub can start processes (llama-server) and write large files to disk, so it is built to be
reachable only by you, and to download only verifiable, non-executable model weights.

## Local-only server

- Binds to `127.0.0.1:8099` (change `listen_host` only if you add your own authentication; the hub
  prints a warning if you do).
- **DNS-rebinding defence:** requests whose `Host` header isn't `127.0.0.1:8099`, `localhost:8099` or
  `[::1]:8099` get `421`.
- **CSRF defence:** every state-changing request must be `POST` with `Content-Type: application/json`
  and `X-Model-Hub: 1`. Browsers can't send those from another site without a CORS preflight, which
  the hub never approves (it sends no CORS headers).
- **Content-Security-Policy** `default-src 'self'` with no inline scripts or styles; all remote text
  (model names, descriptions) is inserted with `textContent`, never as HTML.
- Static files are served only from `web/`; path traversal returns 404.
- Request bodies over 1 MB are rejected.
- Secrets: the Turnstone token lives in `config/turnstone.token` (git-ignored, excluded from
  packages) or an environment variable, is sent only to the configured console URL, and is never
  returned by the API (`/api/info` reports only whether one is set).

## Download safety rules

A download is refused unless **all** of these hold:

1. **Source is enabled** in `config/sources.json` (`trusted` or `reviewed`, not `blocked`).
2. **HTTPS only**, with certificate verification (Python's default trust store).
3. **Host allow-list:** the URL and every redirect hop must match the source's `hosts` patterns
   (e.g. Hugging Face → `huggingface.co`, `*.huggingface.co`, `*.hf.co`). A redirect elsewhere aborts
   the download.
4. **Weights-only file types:** `.gguf` and `.safetensors`. Refused: pickle-based formats
   (`.bin`, `.pt`, `.pth`, `.ckpt`, `.pkl`, `.npy`, …) because loading them can execute code, and
   executables/scripts/archives (`.exe`, `.dll`, `.ps1`, `.sh`, `.py`, `.zip`, …).
5. **SHA-256 required:** from the registry (Hugging Face LFS metadata, ModelScope file listing,
   GitHub release `digest`) or, for direct URLs, supplied by you from the publisher. After download the
   file is hashed; a mismatch keeps it as `*.part.bad` and fails the job.
6. **GGUF magic check:** `.gguf` files must start with `GGUF`.

Only then is the file renamed into place and a `.verified` marker written.

## Checking a new source

"Sources → Check" runs: HTTPS; every DNS address is public (blocks LAN, loopback and cloud-metadata
addresses, preventing the hub from being used to probe your network); TLS certificate valid; redirect
chain stays on HTTPS and allowed hosts; for file URLs, safe file type and checksum availability.
Verdict: **unsafe** (any hard failure — not addable), **caution** (e.g. unknown registry, needs your
checksum), **safe**. Adding a source records it as `reviewed` with only its own host allowed.

## Publisher trust and modified models

Repos are labelled **official** (the lab that made the model), **known quantizer** (ggml-org,
unsloth, bartowski, lmstudio-community, mradermacher) or **community**. Names indicating third-party
modifications (uncensored, abliterated, heretic, derestricted, merges, distills) get a **modified**
label even from known quantizers, because they are not the original model and may have had safety
training removed. Lists are in `config/sources.json` and `hub/discovery.py`.

## What the checks do not cover

- A checksum proves the file is what the repository published, not that the publisher is honest.
  Prefer official and known-quantizer repos.
- GGUF is a data format, but it is parsed by llama.cpp; keep llama.cpp updated for parser fixes.
- Model licences are shown, not enforced. Gated models (licence acceptance required) can't be
  downloaded anonymously.
- The hub has no login. Anyone with access to your Windows session can use it.
