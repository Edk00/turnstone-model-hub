"""Local models: registry entries, file checks, server control, benchmarks."""

from __future__ import annotations

import json
import re
import statistics
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from . import estimates, gguf, hf, net, system, turnstone_api
from .config import CONFIG

_SPLIT = re.compile(r"-00001-of-(\d{5})\.gguf$", re.I)
_meta_cache: dict[str, dict[str, Any]] = {}
_cfg_cache: dict[str, dict[str, Any] | None] = {}
_lock = threading.Lock()


def _benchmarks_path() -> Path:
    return CONFIG.registry_path.with_name("benchmarks.json")


def benchmarks() -> dict[str, Any]:
    try:
        return json.loads(_benchmarks_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def calibration() -> dict[str, Any]:
    """Median ratio measured/estimated decode speed across benchmarked models."""
    ratios = [b["ratio"] for b in benchmarks().values() if b.get("ratio")]
    return {"factor": statistics.median(ratios) if ratios else 1.0, "samples": len(ratios)}


def files_for(entry: dict[str, Any]) -> list[Path]:
    first = CONFIG.models_dir / entry["file"]
    m = _SPLIT.search(first.name)
    if not m:
        return [first]
    n = int(m.group(1))
    return [first.with_name(_SPLIT.sub(f"-{i:05d}-of-{n:05d}.gguf", first.name)) for i in range(1, n + 1)]


def arch_config(entry: dict[str, Any]) -> dict[str, Any] | None:
    """config.json of the base model if public, else metadata from the local GGUF."""
    key = entry["name"]
    with _lock:
        if key in _cfg_cache:
            return _cfg_cache[key]
    cfg = hf.config_json(entry["base_model"]) if entry.get("base_model") else None
    if not cfg:
        meta = local_metadata(entry)
        cfg = gguf.to_config(meta) if meta else None
    with _lock:
        _cfg_cache[key] = cfg
    return cfg


def local_metadata(entry: dict[str, Any]) -> dict[str, Any] | None:
    path = CONFIG.models_dir / entry["file"]
    if not path.exists():
        return None
    key = f"{path}:{path.stat().st_mtime}"
    if key not in _meta_cache:
        try:
            _meta_cache[key] = gguf.read_local(path)
        except (gguf.GGUFError, OSError):
            return None
    return _meta_cache[key]


def describe(entry: dict[str, Any], gpu: dict[str, Any], loaded_gb: float,
             servers: dict[str, Any], ts_aliases: set[str] | None) -> dict[str, Any]:
    paths = files_for(entry)
    present = [p for p in paths if p.exists()]
    size = sum(p.stat().st_size for p in present)
    verified = all(p.with_name(p.name + ".verified").exists() for p in paths) if present else False
    meta = local_metadata(entry) or {}
    arch = meta.get("general.architecture")
    tools = "tools" in (meta.get("tokenizer.chat_template") or "") if meta else None
    params = None
    try:
        info = hf.info(entry["base_model"]) if entry.get("base_model") else {}
        params = info.get("params")
        pipeline, tags, created = info.get("pipeline_tag"), info.get("tags", []), info.get("created")
    except net.FetchError:
        pipeline, tags, created = None, [], None
    cfg = arch_config(entry)
    est = estimates.estimate(name=f"{entry['name']} {entry.get('base_model', '')}", weights_bytes=size or 1,
                             total_params=params, cfg=cfg, ctx=entry["ctx"] // entry["parallel"],
                             parallel=entry["parallel"], gpu=gpu,
                             # a running model's own memory is already counted as used by the GPU counters
                             other_loaded_gb=size / 1024 ** 3 + 2 if servers.get(entry["name"], {}).get("running") else 0)
    cal = calibration()
    bench = benchmarks().get(entry["name"])
    if est["decode_tokens_per_s"]:
        est["decode_tokens_per_s_calibrated"] = est["decode_tokens_per_s"] * cal["factor"]
    return {
        **entry, "files": [p.name for p in paths], "present": len(present) == len(paths),
        "size_gb": size / 1e9, "verified": verified, "architecture": arch,
        "params": params, "created": created,
        "used_for": used_for(pipeline, tags, tools, entry.get("name", "")),
        "supports_tools": tools,
        "server": servers.get(entry["name"], {}),
        "in_turnstone": (entry["name"] in ts_aliases) if ts_aliases is not None else None,
        "estimate": est, "benchmark": bench,
    }


def estimate_for(name: str, ctx: int | None, parallel: int | None, prompt: int | None,
                 output: int | None) -> dict[str, Any]:
    """Estimate for an installed model with user-chosen settings (Estimator tab)."""
    e = _entry(name)
    size = sum(p.stat().st_size for p in files_for(e) if p.exists())
    try:
        params = hf.info(e["base_model"]).get("params") if e.get("base_model") else None
    except net.FetchError:
        params = None
    est = estimates.estimate(name=f"{name} {e.get('base_model', '')}", weights_bytes=size or 1, total_params=params,
                             cfg=arch_config(e), ctx=ctx or e["ctx"] // e["parallel"],
                             parallel=parallel or e["parallel"], gpu=system.gpu(),
                             prompt_tokens=prompt, output_tokens=output)
    cal = calibration()["factor"]
    bench = benchmarks().get(name) or {}
    tg = bench.get("decode_tokens_per_s") or (est["decode_tokens_per_s"] or 0) * cal or None
    pp = bench.get("prefill_tokens_per_s") if (bench.get("prefill_tokens_per_s") or 0) > (est["prefill_tokens_per_s"] or 0) else est["prefill_tokens_per_s"]
    turn = (est["agent_turn"]["prompt_tokens"] / pp + est["agent_turn"]["output_tokens"] / tg) if (tg and pp) else None
    est["best_decode_tokens_per_s"] = tg
    est["best_decode_source"] = "measured" if bench.get("decode_tokens_per_s") else ("calibrated" if cal != 1.0 else "estimated")
    est["agent_turn"]["seconds_best"] = turn
    est["agent_turn"]["energy_cost"] = estimates.local_energy_cost(turn)
    return {"name": name, "size_gb": size / 1e9, "estimate": est}


def used_for(pipeline: str | None, tags: list[str], tools: bool | None, name: str) -> list[str]:
    out = []
    t = {x.lower() for x in tags}
    n = name.lower()
    if pipeline in ("image-text-to-text", "any-to-any") or "vision" in t or "multimodal" in t:
        out.append("vision + text")
    if pipeline == "text-generation" or "conversational" in t:
        out.append("chat")
    if tools:
        out.append("tool calling / agents")
    if any(k in t or k in n for k in ("code", "coder", "devstral")):
        out.append("coding")
    if any(k in t or k in n for k in ("reasoning", "thinking", "-r1", "gpt-oss")):
        out.append("reasoning")
    if "embedding" in n or pipeline in ("feature-extraction", "sentence-similarity"):
        out = ["embeddings"]
    return out or [pipeline or "text generation"]


def list_all() -> dict[str, Any]:
    snap_gpu = system.gpu()
    servers = system.model_servers()
    entries = CONFIG.registry()
    sizes ={e["name"]: sum(p.stat().st_size for p in files_for(e) if p.exists()) / 1e9 for e in entries}
    loaded = sum(sizes[n] for n, s in servers.items() if s.get("running"))
    ts_aliases, ts_default, ts_error = None, None, None
    if turnstone_api.has_token():
        try:
            defs = turnstone_api.model_definitions()
            ts_aliases = {m["alias"] for m in defs["models"]}
            ts_default = defs["default_alias"]
        except turnstone_api.TurnstoneError as e:
            ts_error = str(e)
    models = [describe(e, snap_gpu, loaded, servers, ts_aliases) for e in entries]
    return {"models": models, "loaded_gb": loaded, "calibration": calibration(),
            "turnstone": {"token": turnstone_api.has_token(), "default_alias": ts_default, "error": ts_error}}


# -- server control -------------------------------------------------------------
_wsl_ip: tuple[float, str | None] = (0.0, None)


def wsl_ip() -> str | None:
    global _wsl_ip
    if time.time() - _wsl_ip[0] < 60:
        return _wsl_ip[1]
    out = system._run(["powershell", "-NoProfile", "-Command",
                       "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -like 'vEthernet (WSL*' } "
                       "| Select-Object -First 1).IPAddress"], timeout=20).strip()
    ip = out if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", out) else None
    _wsl_ip = (time.time(), ip)
    return ip


def _entry(name: str) -> dict[str, Any]:
    for e in CONFIG.registry():
        if e["name"] == name:
            return e
    raise KeyError(name)


def start(name: str) -> dict[str, Any]:
    e = _entry(name)
    model = CONFIG.models_dir / e["file"]
    if not model.exists():
        raise FileNotFoundError(f"{model.name} is not downloaded")
    if system.model_servers().get(name, {}).get("running"):
        return {"status": "already running", "port": e["port"]}
    hosts = "127.0.0.1" + (f",{wsl_ip()}" if wsl_ip() else "")
    CONFIG.logs_dir.mkdir(parents=True, exist_ok=True)
    args = [str(CONFIG.llama_server), "-m", str(model), "--alias", name, "--host", hosts,
            "--port", str(e["port"]), "-ngl", "999", "-c", str(e["ctx"]), "-np", str(e["parallel"]), "--jinja"]
    out = open(CONFIG.logs_dir / f"{name}.out.log", "ab")
    err = open(CONFIG.logs_dir / f"{name}.log", "ab")
    flags = system.CREATE_NO_WINDOW | (0x00000008 if system.IS_WINDOWS else 0)  # DETACHED_PROCESS
    proc = subprocess.Popen(args, stdout=out, stderr=err, stdin=subprocess.DEVNULL, creationflags=flags)
    return {"status": "starting", "pid": proc.pid, "port": e["port"], "hosts": hosts}


def stop(name: str) -> dict[str, Any]:
    e = _entry(name)
    out = system._run(["powershell", "-NoProfile", "-Command",
                       f"$c = Get-NetTCPConnection -LocalPort {int(e['port'])} -State Listen -ErrorAction SilentlyContinue; "
                       "if ($c) { $c.OwningProcess | Select-Object -Unique | ForEach-Object { "
                       "if ((Get-Process -Id $_).ProcessName -eq 'llama-server') { Stop-Process -Id $_ -Force; 'stopped' } } }"],
                      timeout=20)
    return {"status": "stopped" if "stopped" in out else "not running"}


def benchmark(name: str, n_predict: int = 256) -> dict[str, Any]:
    e = _entry(name)
    body = {"prompt": "Write a detailed explanation of how a hash map works, with an example in Python.",
            "n_predict": n_predict, "cache_prompt": False}
    r = net.get_json(f"http://127.0.0.1:{e['port']}/completion", method="POST", body=body, timeout=600)
    t = r.get("timings") or {}
    result = {"time": time.time(), "decode_tokens_per_s": t.get("predicted_per_second"),
              "prefill_tokens_per_s": t.get("prompt_per_second"), "tokens": t.get("predicted_n")}
    desc = describe(e, system.gpu(), 0, system.model_servers(), None)
    est = desc["estimate"].get("decode_tokens_per_s")
    if est and result["decode_tokens_per_s"]:
        result["estimated_decode_tokens_per_s"] = est
        result["ratio"] = result["decode_tokens_per_s"] / est
    data = benchmarks()
    data[name] = result
    _benchmarks_path().write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return result


def endpoint_url(name: str) -> str:
    """The base URL Turnstone's containers use to reach this model's server."""
    e = _entry(name)
    ip = wsl_ip()
    if not ip:
        raise RuntimeError("WSL adapter address not found; start WSL/Turnstone first")
    return f"http://{ip}:{e['port']}/v1"


def register_in_turnstone(name: str) -> dict[str, Any]:
    e = _entry(name)
    return turnstone_api.register(alias=name, model=name, base_url=endpoint_url(name),
                                  context_window=e["ctx"] // e["parallel"])


def reach_test(name: str) -> dict[str, Any]:
    """Check the model server is reachable the way Turnstone reaches it: from inside a
    Turnstone node container if the stack is running, otherwise from the WSL VM
    (same network path up to the container bridge)."""
    url = endpoint_url(name).removesuffix("/v1") + "/health"
    ts = CONFIG.hub["turnstone"]
    distro = ts["wsl_distro"]
    if system.turnstone_status().get("up"):
        code = ("import urllib.request,sys\n"
                f"try:\n print(urllib.request.urlopen('{url}', timeout=5).status)\n"
                "except Exception as e:\n print('ERR', e)")
        out = system._run(["wsl.exe", "-d", distro, "--cd", "~/turnstone", "--", "docker", "compose", "exec", "-T",
                           "node-1", "python", "-c", code], timeout=60)
        origin = "Turnstone node-1 container"
    else:
        out = system._run(["wsl.exe", "-d", distro, "--", "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                           "-m", "5", url], timeout=30)
        origin = "WSL (Turnstone not running; containers use the same route)"
    out = out.strip()
    return {"url": url, "from": origin, "ok": out.endswith("200"), "detail": out[-300:]}


def turnstone_endpoints() -> list[dict[str, Any]]:
    """Compare Turnstone's model definitions with where local models actually listen."""
    defs = turnstone_api.model_definitions()["models"]
    by_alias = {d["alias"]: d for d in defs}
    out = []
    for e in CONFIG.registry():
        d = by_alias.get(e["name"])
        if not d:
            continue
        expected = endpoint_url(e["name"])
        out.append({"alias": e["name"], "definition_id": d.get("definition_id"), "current": d.get("base_url"),
                    "expected": expected, "ok": (d.get("base_url") or "").rstrip("/") == expected})
    return out


def repair_turnstone_endpoints() -> list[dict[str, Any]]:
    """Point stale Turnstone definitions at the current WSL adapter address."""
    fixed = []
    for item in turnstone_endpoints():
        if not item["ok"] and item["definition_id"]:
            turnstone_api.update(item["definition_id"], {"base_url": item["expected"]})
            fixed.append(item)
    if fixed:
        turnstone_api.reload()
    return fixed


def make_available(name: str, timeout: float = 900) -> dict[str, Any]:
    """Start the server, wait until it is healthy, check reachability, register in Turnstone."""
    start(name)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if system.model_servers().get(name, {}).get("running"):
            break
        time.sleep(5)
    else:
        raise RuntimeError(f"{name} did not become ready within {timeout:.0f}s")
    reach = reach_test(name)
    result: dict[str, Any] = {"reach": reach}
    if turnstone_api.has_token() and system.turnstone_status().get("up"):
        result["registered"] = register_in_turnstone(name)
    return result
