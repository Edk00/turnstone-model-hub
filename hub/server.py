"""HTTP server: static dashboard + JSON API. See docs/API.md.

Security model (docs/SECURITY.md): binds to 127.0.0.1 only; rejects requests whose
Host header is not this server (DNS-rebinding defence); state-changing requests
must be POST with Content-Type application/json and the header "X-Model-Hub: 1",
which a cross-site page cannot send without a CORS preflight that we never approve.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import traceback
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import (__version__, discovery, downloads, export, github, hf, local_models, modelscope, net,
               sources, system, turnstone_api)
from .config import CONFIG, ROOT

WEB = ROOT / "web"
Handler = Callable[["Request"], Any]


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Request:
    def __init__(self, path: str, query: dict[str, str], body: dict[str, Any], params: dict[str, str]):
        self.path, self.query, self.body, self.params = path, query, body, params

    def q(self, key: str, default: str = "") -> str:
        return self.query.get(key, default)


ROUTES: list[tuple[str, re.Pattern[str], Handler]] = []


def route(method: str, pattern: str) -> Callable[[Handler], Handler]:
    rx = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def deco(fn: Handler) -> Handler:
        ROUTES.append((method, rx, fn))
        return fn
    return deco


# -- API ----------------------------------------------------------------------------
@route("GET", "/api/info")
def _info(r: Request) -> Any:
    return {"version": __version__, "turnstone_token": turnstone_api.has_token(),
            "hardware": CONFIG.hub["hardware"], "estimates": CONFIG.hub["estimates"],
            "github_token": bool(os.environ.get("GITHUB_TOKEN"))}


@route("GET", "/api/system")
def _system(r: Request) -> Any:
    return system.snapshot()


@route("GET", "/api/hardware")
def _hardware(r: Request) -> Any:
    from . import hardware
    return {**hardware.summary(), "budget": system.gpu()}


@route("POST", "/api/hardware/redetect")
def _redetect(r: Request) -> Any:
    from . import hardware
    hardware.static_info(force=True)
    return {**hardware.summary(), "budget": system.gpu()}


def _require_unified() -> None:
    from . import hardware
    if not hardware.is_unified():
        raise ApiError(409, "RAM split planning needs a unified-memory (integrated) GPU; this PC's GPU has its own memory")


def _plan_args(src: dict[str, Any]) -> tuple[float, float]:
    from . import hardware
    _require_unified()
    info = hardware.static_info()
    try:
        reserved = float(src.get("gpu_reserved_gb", src.get("reserved", "")))
        fraction = float(src.get("shared_fraction", src.get("fraction", "")))
    except (TypeError, ValueError) as e:
        raise ApiError(400, "gpu_reserved_gb and shared_fraction must be numbers") from e
    installed = info.get("installed_ram_gb") or 0
    if not 0 <= reserved <= installed - 8:
        raise ApiError(400, f"GPU reservation must be between 0 and {installed - 8:.0f} GB (Windows needs at least 8 GB)")
    if not 0.1 <= fraction <= 1.0:
        raise ApiError(400, "shared fraction must be between 0.1 and 1.0")
    return reserved, fraction


@route("GET", "/api/hardware/split")
def _split(r: Request) -> Any:
    """Current RAM split, and a what-if split with per-model fit (?reserved=GB&fraction=0-1)."""
    from . import hardware
    _require_unified()
    current = hardware.split()
    if r.q("reserved"):
        reserved, fraction = _plan_args(r.query)
    else:
        reserved, fraction = current["gpu_reserved_gb"], current["shared_fraction"]
    pb = hardware.planned_budget(reserved, fraction)
    models = []
    for e in CONFIG.registry():
        if not (CONFIG.models_dir / e["file"]).exists():
            continue
        need = local_models.estimate_for(e["name"], None, None, None, None)["estimate"]["memory_needed_gb"]
        models.append({"name": e["name"], "need_gb": need,
                       "fit": "fits in GPU-reserved memory" if need <= pb["from_reserved_gb"]
                       else "fits using shared memory" if need <= pb["gpu_capacity_gb"] else "does not fit"})
    return {"current": current, "planned": pb, "models": models,
            "saved_plan": CONFIG.hub["hardware"].get("plan")}


@route("POST", "/api/hardware/plan")
def _save_plan(r: Request) -> Any:
    enabled = bool(r.body.get("enabled"))
    if enabled:
        _require_unified()
    plan = dict(CONFIG.hub["hardware"].get("plan") or {})
    if enabled:
        reserved, fraction = _plan_args(r.body)
        plan.update(gpu_reserved_gb=reserved, shared_fraction=fraction)
    plan["enabled"] = enabled
    CONFIG.hub["hardware"]["plan"] = plan
    CONFIG.save_hub()
    return plan


@route("GET", "/api/monitor")
def _monitor(r: Request) -> Any:
    from .monitor import MONITOR
    since = r.q("since")
    return MONITOR.status(float(since) if since.replace(".", "", 1).isdigit() else None)


@route("POST", "/api/monitor")
def _monitor_start(r: Request) -> Any:
    from .monitor import MONITOR
    mode = str(r.body.get("mode", ""))
    if mode == "stopped":
        return MONITOR.stop()
    try:
        return MONITOR.start(mode, r.body.get("seconds"), r.body.get("interval"))
    except (ValueError, TypeError) as e:
        raise ApiError(400, str(e)) from e


@route("GET", "/api/local")
def _local(r: Request) -> Any:
    return local_models.list_all()


@route("POST", "/api/local/{name}/start")
def _start(r: Request) -> Any:
    return local_models.start(r.params["name"])


@route("POST", "/api/local/{name}/stop")
def _stop(r: Request) -> Any:
    return local_models.stop(r.params["name"])


@route("POST", "/api/local/{name}/benchmark")
def _bench(r: Request) -> Any:
    return local_models.benchmark(r.params["name"])


@route("POST", "/api/local/{name}/register")
def _register(r: Request) -> Any:
    try:
        return local_models.register_in_turnstone(r.params["name"])
    except turnstone_api.TurnstoneError as e:
        raise ApiError(502, str(e)) from e


@route("POST", "/api/local/{name}/reach-test")
def _reach(r: Request) -> Any:
    return local_models.reach_test(r.params["name"])


@route("POST", "/api/local/{name}/make-available")
def _make_available(r: Request) -> Any:
    return local_models.make_available(r.params["name"])


@route("GET", "/api/turnstone/endpoints")
def _ts_endpoints(r: Request) -> Any:
    try:
        return local_models.turnstone_endpoints()
    except turnstone_api.TurnstoneError as e:
        raise ApiError(502, str(e)) from e


@route("POST", "/api/turnstone/repair")
def _ts_repair(r: Request) -> Any:
    try:
        return {"fixed": local_models.repair_turnstone_endpoints()}
    except turnstone_api.TurnstoneError as e:
        raise ApiError(502, str(e)) from e


@route("GET", "/api/discover/successors")
def _succ(r: Request) -> Any:
    return discovery.successors()


@route("GET", "/api/discover/popular")
def _popular(r: Request) -> Any:
    return discovery.popular()


@route("GET", "/api/discover/watchlist")
def _watch(r: Request) -> Any:
    return discovery.watchlist()


@route("GET", "/api/search")
def _search(r: Request) -> Any:
    q = r.q("q").strip()
    if not q:
        raise ApiError(400, "q is required")
    return discovery.search(q, r.q("source", "huggingface"), r.q("gguf", "1") == "1")


@route("GET", "/api/repo")
def _repo(r: Request) -> Any:
    repo = r.q("id")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ApiError(400, "id must look like owner/name")
    return discovery.repo_details(repo, r.q("source", "huggingface"))


@route("GET", "/api/github/releases")
def _releases(r: Request) -> Any:
    repo = r.q("repo")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ApiError(400, "repo must look like owner/name")
    rels = github.releases(repo)
    for rel in rels:
        for a in rel["assets"]:
            a["verdict"], a["why"] = sources.file_type_verdict(a["name"])
    return rels


@route("GET", "/api/sources")
def _sources(r: Request) -> Any:
    return {"sources": CONFIG.sources(), "trusted_publishers": CONFIG.sources_doc.get("trusted_publishers")}


@route("POST", "/api/sources/check")
def _check(r: Request) -> Any:
    url = str(r.body.get("url", "")).strip()
    if not url:
        raise ApiError(400, "url is required")
    return sources.check_url(url)


@route("POST", "/api/sources")
def _add_source(r: Request) -> Any:
    try:
        return sources.add_source(str(r.body.get("url", "")), str(r.body.get("name", "")))
    except ValueError as e:
        raise ApiError(400, str(e)) from e


@route("POST", "/api/sources/{id}/trust")
def _trust(r: Request) -> Any:
    try:
        return sources.set_trust(r.params["id"], str(r.body.get("trust", "")))
    except (ValueError, KeyError) as e:
        raise ApiError(400, str(e)) from e


@route("GET", "/api/downloads")
def _downloads(r: Request) -> Any:
    return downloads.manager().list()


@route("POST", "/api/downloads/{id}/cancel")
def _cancel(r: Request) -> Any:
    downloads.manager().cancel(r.params["id"])
    return {"ok": True}


@route("POST", "/api/downloads")
def _download(r: Request) -> Any:
    return start_download(r.body)


@route("GET", "/api/turnstone/models")
def _ts_models(r: Request) -> Any:
    try:
        return turnstone_api.model_definitions()
    except turnstone_api.TurnstoneError as e:
        raise ApiError(502, str(e)) from e


@route("GET", "/api/compare")
def _compare(r: Request) -> Any:
    from . import estimates

    def num(key: str, lo: int, hi: int) -> int | None:
        v = r.q(key)
        if not v:
            return None
        if not v.isdigit() or not lo <= int(v) <= hi:
            raise ApiError(400, f"{key} must be between {lo} and {hi}")
        return int(v)
    ctx, par = num("ctx", 1024, 1_048_576), num("parallel", 1, 16)
    prompt, output = num("prompt", 1, 1_000_000), num("output", 1, 200_000)
    local = [local_models.estimate_for(e["name"], ctx, par, prompt, output)
             for e in CONFIG.registry() if (CONFIG.models_dir / e["file"]).exists()]
    return {"local": local, "calibration": local_models.calibration(),
            "cloud": [estimates.cloud_estimate(c, prompt, output) for c in CONFIG.pricing.get("cloud", [])],
            "pricing": {k: v for k, v in CONFIG.pricing.items() if k != "cloud"},
            "defaults": CONFIG.hub["estimates"]}


# -- downloads ------------------------------------------------------------------------
def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", s.lower()).strip("-")[:60]


def _family_for(name: str) -> str | None:
    for fam in CONFIG.watchlist["families"]:
        if re.search(fam["match"], name.split("/")[-1]):
            return fam["id"]
    return None


def start_download(body: dict[str, Any]) -> dict[str, Any]:
    source = body.get("source", "huggingface")
    models_dir = CONFIG.models_dir
    src_cfg = next((s for s in CONFIG.sources() if s["id"] == source), None)
    if source in ("huggingface", "modelscope", "github") and (not src_cfg or src_cfg.get("trust") == "blocked"):
        raise ApiError(403, f"source {source} is not enabled")
    registry_entry: dict[str, Any] | None = None

    if source in ("huggingface", "modelscope"):
        repo, variant = body.get("repo", ""), body.get("variant", "")
        files = (modelscope.tree(repo) if source == "modelscope" else hf.tree(repo))
        group = next((g for g in hf.group_gguf(files) if g["variant"] == variant and not g.get("extras")), None)
        if not group:
            raise ApiError(404, f"variant {variant} not found in {repo}")
        url_for = (lambda p: modelscope.file_url(repo, p)) if source == "modelscope" else (lambda p: hf.file_url(repo, p))
        items = [{"url": url_for(f["path"]), "dest": str(models_dir / Path(f["path"]).name),
                  "size": f["size"], "sha256": f["sha256"]} for f in group["files"]]
        base = None
        if source == "huggingface":
            try:
                base = (hf.info(repo).get("base_models") or [None])[0]
            except net.FetchError:
                pass
        registry_entry = {"name": body.get("name") or _slug(variant), "file": Path(group["first_file"]).name,
                          "repo": repo, "base_model": base or repo, "family": _family_for(base or repo),
                          "source": source}
        title = f"{repo} - {variant}"
    elif source == "github":
        repo, tag, asset = body.get("repo", ""), body.get("tag", ""), body.get("asset", "")
        rel = next((x for x in github.releases(repo, 10) if x["tag"] == tag), None)
        a = next((x for x in (rel or {}).get("assets", []) if x["name"] == asset), None)
        if not a:
            raise ApiError(404, f"asset {asset} not found in {repo}@{tag}")
        items = [{"url": a["url"], "dest": str(models_dir / a["name"]), "size": a["size"], "sha256": a["sha256"]}]
        if a["name"].lower().endswith(".gguf"):
            registry_entry = {"name": body.get("name") or _slug(Path(a["name"]).stem), "file": a["name"],
                              "repo": f"github:{repo}@{tag}", "source": "github"}
        title = f"{repo}@{tag} - {asset}"
    else:  # direct URL from a reviewed source
        url, sha = str(body.get("url", "")), str(body.get("sha256", "")).lower()
        src = sources.source_for_url(url)
        if not src:
            raise ApiError(403, "URL host is not in an enabled source; add and review the source first")
        check = sources.check_url(url)
        if check["verdict"] == "unsafe":
            raise ApiError(400, "URL failed safety checks")
        name = Path(urllib.parse.urlsplit(url).path).name
        items = [{"url": url, "dest": str(models_dir / name), "size": check.get("size"), "sha256": sha}]
        src_cfg = src
        if name.lower().endswith(".gguf"):
            registry_entry = {"name": body.get("name") or _slug(Path(name).stem), "file": name,
                              "repo": url, "source": src["id"]}
        title = name

    existing = {m["name"]: m for m in CONFIG.registry()}
    if registry_entry and registry_entry["name"] in existing and existing[registry_entry["name"]]["file"] != registry_entry["file"]:
        raise ApiError(409, f"a different model is already registered as {registry_entry['name']}; choose another name")

    make_available = bool(body.get("make_available"))

    def on_done(job: downloads.Job) -> None:
        if not registry_entry:
            return
        job.result = {"registered": CONFIG.save_registry_entry(dict(registry_entry))}
        if make_available:  # start the server, check Turnstone can reach it, register it
            job.current = "starting model server"
            try:
                job.result["available"] = local_models.make_available(registry_entry["name"])
            except Exception as e:  # download itself succeeded; report the follow-up problem
                job.result["available_error"] = str(e)

    try:
        job = downloads.manager().submit(title, items, src_cfg["hosts"], on_done)
    except downloads.DownloadError as e:
        raise ApiError(400, str(e)) from e
    return job.to_dict()


@route("GET", "/api/export.json")
def _export_json(r: Request) -> Any:
    extra = {}
    if r.q("full") == "1":
        extra = {"watchlist": discovery.watchlist(), "successors": discovery.successors()}
    return export.snapshot(extra)


# -- HTTP plumbing ------------------------------------------------------------------
class RequestHandler(BaseHTTPRequestHandler):
    server_version = f"ModelHub/{__version__}"

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter console
        if "/api/" in (args[0] if args else "") and " 200 " not in fmt % args:
            super().log_message(fmt, *args)

    def _allowed_host(self) -> bool:
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}")

    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data, default=str).encode(), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        if not self._allowed_host():
            return self._json(421, {"error": "unexpected Host header"})
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path
        query = dict(urllib.parse.parse_qsl(parts.query))
        if method == "GET" and not path.startswith("/api/"):
            return self._static(path)
        if path == "/api/export.csv" or path == "/api/export.html":
            snap = export.snapshot({"watchlist": discovery.watchlist()} if query.get("full") == "1" else None)
            if path.endswith(".csv"):
                body, ctype, ext = export.to_csv(snap).encode(), "text/csv; charset=utf-8", "csv"
            else:
                body, ctype, ext = export.to_html(snap).encode(), "text/html; charset=utf-8", "html"
            return self._send(200, body, ctype, {"Content-Disposition": f'attachment; filename="model-hub-report.{ext}"'})
        body: dict[str, Any] = {}
        if method == "POST":
            if self.headers.get("X-Model-Hub") != "1" or "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._json(403, {"error": "missing X-Model-Hub header or JSON content type"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1_000_000:
                return self._json(413, {"error": "request too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json(400, {"error": "invalid JSON"})
        for m, rx, fn in ROUTES:
            match = rx.match(path)
            if m == method and match:
                try:
                    return self._json(200, fn(Request(path, query, body, match.groupdict())))
                except ApiError as e:
                    return self._json(e.status, {"error": str(e)})
                except KeyError as e:
                    return self._json(404, {"error": f"not found: {e}"})
                except (FileNotFoundError, RuntimeError, net.FetchError, turnstone_api.TurnstoneError) as e:
                    return self._json(502 if isinstance(e, net.FetchError) else 400, {"error": str(e)})
                except Exception as e:  # pragma: no cover - surfaced to the UI
                    traceback.print_exc()
                    return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        self._json(404, {"error": "no such endpoint"})

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (WEB / rel).resolve()
        if WEB.resolve() not in target.parents or not target.is_file():
            return self._json(404, {"error": "not found"})
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)


def serve() -> None:
    host, port = CONFIG.hub["listen_host"], CONFIG.hub["listen_port"]
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: listening on {host}; Model Hub has no login and can start processes. See docs/SECURITY.md.")
    system.start_background_refresh()
    downloads.manager()
    httpd = ThreadingHTTPServer((host, port), RequestHandler)
    print(f"Model Hub {__version__} on http://{host}:{port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
