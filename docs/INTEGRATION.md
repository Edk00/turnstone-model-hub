# Integrating Model Hub with Turnstone

Turnstone 1.8.5 has no plugin or custom-tab mechanism, so Model Hub runs as a **companion app**
next to Turnstone and talks to it over the console's admin API. This document covers (1) connecting
the two today, (2) day-to-day use with Turnstone, and (3) how to merge Model Hub into Turnstone as a
native console tab later.

## 1. Connect to Turnstone (API token)

Model Hub needs a Turnstone API token to show which models are registered and to register new ones.
Create it yourself (it is a credential for your admin account):

```bash
# find your admin user id
wsl -d Ubuntu-24.04 --cd ~/turnstone -- docker compose exec node-1 turnstone-admin list-users
# create a token; 'approve' scope is required by the model-definition endpoints
wsl -d Ubuntu-24.04 --cd ~/turnstone -- docker compose exec node-1 turnstone-admin create-token --user <full-user-id> --name model-hub --scopes read,write,approve
```

`list-users` truncates ids; the full id is in the `users` table
(`docker compose exec postgres psql -U turnstone -d turnstone -c "select user_id, username from users"`).

Save the printed token (the `ts_...` value) as a single line in `model-hub/config/turnstone.token`
(git-ignored, excluded from `package.ps1`), or set the environment variable
`MODEL_HUB_TURNSTONE_TOKEN` before starting the hub. Restart the hub. The Installed tab then shows
"registered / not registered" and enables **Register**. Revoke with
`turnstone-admin revoke-token --token-id <id>`.

The console URL defaults to `http://127.0.0.1:8090` (`config/hub.json` → `turnstone.console_url`).

## 2. Using it with Turnstone

1. Start Turnstone (`turnstone-start.ps1`) — this also brings up WSL, whose adapter address the model
   servers listen on.
2. Start Model Hub (`run-hub.ps1`).
3. Installed tab → **Start** a model → **Benchmark** (optional, improves estimates) → **Register**.
   Registration creates an `openai-compatible` definition with `base_url = http://<WSL adapter IP>:<port>/v1`
   and `context_window = ctx / parallel`, then reloads all nodes.
4. Choose the default model in Turnstone's Models tab (Model Hub does not change Turnstone's default).

The download dialog's "When finished, start it, test that Turnstone can reach it and register it"
option does steps 3's start, reach test and register automatically after a download.

If Windows assigns a different WSL adapter address after a reboot, the Installed tab shows the
registered models as **stale**; **Fix in Turnstone** updates their base URLs (PUT
`/v1/api/admin/model-definitions/{id}`) and reloads the nodes. **Test reach** confirms the model server
answers from inside Turnstone's node container.

Model files are **not** uploaded into Turnstone — Turnstone stores only endpoint definitions. The
weights stay in `llm/models` on Windows and are served by llama-server.

## 3. Merging into Turnstone as a native tab (design notes)

Model Hub has two halves with different homes:

| Part | Where it belongs | Why |
|---|---|---|
| Discovery, search, safety checks, estimates, export (`hf.py`, `github.py`, `modelscope.py`, `sources.py`, `estimates.py`, `gguf.py`, `discovery.py`, `export.py`) | Turnstone **console** | Pure functions over HTTP APIs; no host access needed. |
| Host resources, llama-server start/stop/benchmark, downloads to the model folder (`system.py`, `local_models.py`, `downloads.py`) | A small **host agent** on each GPU machine | The console runs in a container and can't see the host GPU, files or processes. In a multi-host Turnstone cluster, each GPU host would run one agent. |

Suggested path:

1. **Keep Model Hub running as the host agent** (it already exposes everything as JSON). Bind it to the
   WSL adapter address instead of 127.0.0.1 and add a shared-secret header check so only the console can
   call it (the console already reaches the host via `host.docker.internal`/the adapter address).
2. **Console backend:** add Starlette routes under `/v1/api/admin/model-hub/*` in
   `turnstone/console/server.py` that (a) call the discovery/estimate modules directly — they're
   standard-library Python and can be vendored as `turnstone/console/model_hub/` — and (b) proxy host
   actions to the agent. Guard every route with `require_permission(request, "admin.models")`, as the
   existing model-definition routes do, and record audits with `record_audit` for start/stop/download.
3. **Registration:** replace `turnstone_api.register()` with a direct call to the storage layer
   (`storage.create_model_definition` equivalent used by `admin_create_model_definition`) followed by the
   existing node-reload helper — no API token needed inside the console.
4. **Console UI:** add a "Model Hub" admin tab in `turnstone/console/static/` reusing `web/app.js`
   functions. The front end already uses only `fetch` + DOM APIs and the console's CSP is compatible if
   the code is served as a static file (no inline scripts or styles are used).
5. **Multi-node:** the resource bar would list each host agent; downloads target a chosen host;
   estimates use that host's GPU numbers.
6. **Upstreaming:** Turnstone is Apache-2.0 and so is this code; open a discussion/PR at
   github.com/turnstonelabs/turnstone describing the agent + tab split.

## Optional: serve the dashboard under Turnstone's HTTPS address

Caddy (in the Turnstone stack) could proxy `https://localhost/hub/` to the hub, but **don't do this
without authentication**: the hub can start processes and download files, and Caddy's port is bound
on all WSL interfaces. If you need it, put Caddy's `forward_auth` in front, pointed at a console
endpoint that validates the Turnstone session cookie, and run the hub with a shared secret. Until then,
keep using `http://127.0.0.1:8099`.
