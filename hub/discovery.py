"""Model discovery: successors of installed models, popular models, watchlist, repo details."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import estimates, gguf, github, hf, modelscope, net, sources, system
from .config import CONFIG

_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="discover")


def _safe(fn, *a, default=None, **k):
    try:
        return fn(*a, **k)
    except (net.FetchError, gguf.GGUFError, OSError, KeyError, ValueError) as e:
        return default if default is not None else {"error": str(e)}


def _tier(repo_id: str) -> str:
    return sources.publisher_tier(repo_id.split("/")[0])


# Third-party fine-tunes that remove safety training or merge other models. A known
# quantizer may publish these too, so they are flagged separately from the publisher tier.
_MODIFIED = re.compile(r"(?i)(uncensored|abliterat|heretic|derestrict|nsfw|jailbreak|merge|frankenmerge|distill)")


def _annotate(m: dict[str, Any]) -> dict[str, Any]:
    m["publisher"] = _tier(m["id"])
    m["modified"] = bool(_MODIFIED.search(m["id"]))
    return m


def recommend(variants: list[dict[str, Any]], gpu: dict[str, Any]) -> dict[str, Any] | None:
    """Pick the variant to suggest (variants must already carry an 'estimate'):
      1. the largest 3.5-6.6 bit quant that fits in dedicated GPU memory;
      2. else the 3.5-6.6 bit quant closest to ~4.6 bits that fits in usable memory
         (quality of a 4-bit quant without crowding out everything else);
      3. else the largest variant that fits at all."""
    real = [v for v in variants if not v.get("extras") and v.get("estimate")]
    fit = lambda v: v["estimate"]["fit"]  # noqa: E731
    bpw = lambda v: v["estimate"].get("bits_per_weight") or 0  # noqa: E731
    sweet = [v for v in real if 3.5 <= bpw(v) <= 6.6]
    in_dedicated = [v for v in sweet if fit(v).startswith("fits in dedicated")]
    if in_dedicated:
        return max(in_dedicated, key=lambda v: v["size"])
    # next best: GPU shared memory (integrated), then partial CPU offload (discrete / CPU only)
    for tier in ("fits using shared", "partial CPU offload", "CPU only"):
        pool = [v for v in sweet if fit(v).startswith(tier)]
        if pool:
            return min(pool, key=lambda v: abs(bpw(v) - 4.6))
    # Only very low-bit files fit: take the largest that still leaves 10% headroom
    # (a model at 99% of memory leaves nothing for Windows spikes or longer prompts).
    fitting = [v for v in real if fit(v) != "does not fit"]
    if not fitting:
        return None
    limit = max((gpu.get("offload_capacity_gb") if gpu.get("kind") != "integrated" else gpu.get("gpu_capacity_gb")) or 0, 1)
    roomy = [v for v in fitting if v["estimate"].get("memory_needed_gb", 0) <= 0.9 * limit]
    return max(roomy or fitting, key=lambda v: v["size"])


def best_gguf_repo(base: str) -> dict[str, Any] | None:
    """Most-downloaded GGUF conversion of ``base``, preferring official/known quantizers."""
    if base.lower().endswith("gguf"):  # the official repo is already GGUF
        return {"id": base, "publisher": _tier(base), "alternatives": []}
    repos = _safe(hf.quantized_of, base, 10, default=[])
    if not repos:
        return None
    for r in repos:
        r["publisher"] = _tier(r["id"])
    trusted = [r for r in repos if r["publisher"] != "community"]
    best = (trusted or repos)[0]
    return {**best, "alternatives": [r["id"] for r in repos if r["id"] != best["id"]][:4]}


# -- repo details with per-variant estimates -------------------------------------
def repo_details(repo: str, source: str = "huggingface") -> dict[str, Any]:
    gpu = system.gpu()
    if source == "modelscope":
        files = modelscope.tree(repo)
        info: dict[str, Any] = {"id": repo, "source": "modelscope", "url": f"{modelscope.SITE}/models/{repo}"}
        url_for = lambda p: modelscope.file_url(repo, p)  # noqa: E731
    else:
        info = hf.info(repo)
        files = hf.tree(repo)
        url_for = lambda p: hf.file_url(repo, p)  # noqa: E731
    variants = hf.group_gguf(files)
    real = [v for v in variants if not v.get("extras")]
    _annotate(info)

    # Architecture details: base model config.json, else the smallest GGUF's header.
    cfg = None
    for base in info.get("base_models") or []:
        cfg = hf.config_json(base)
        if cfg:
            info["config_from"] = f"{base}/config.json"
            break
    if not cfg and real:
        meta = _safe(gguf.read_remote, url_for(real[0]["first_file"]), default={})
        if meta and "error" not in meta:
            cfg = gguf.to_config(meta)
            info["config_from"] = "GGUF header"
            info.setdefault("supports_tools", cfg.get("chat_template_has_tools"))
            if not info.get("architecture"):
                info["architecture"] = cfg.get("model_type")
    params = info.get("params") or info.get("gguf_params")
    for v in real:
        v["estimate"] = estimates.estimate(name=f"{repo} {v['variant']}", weights_bytes=v["size"],
                                           total_params=params, cfg=cfg, gpu=gpu)
        v["safe_files"] = all(sources.file_type_verdict(f["path"])[0] == "ok" for f in v["files"])
        v["checksums"] = all(f.get("sha256") for f in v["files"])
    rec = recommend(real, gpu)
    if rec:
        rec["recommended"] = True
    info["variants"] = variants
    info["has_vision_projector"] = any(f.get("kind") == "vision projector"
                                       for v in variants if v.get("extras") for f in v["files"])
    if info["has_vision_projector"]:
        info["has_vision"] = True
    safetensors = [f for f in files if f["path"].endswith(".safetensors")]
    info["safetensors_gb"] = sum(f["size"] for f in safetensors) / 1e9 if safetensors else None
    info["used_for"] = _used_for(info)
    return info


def _used_for(info: dict[str, Any]) -> list[str]:
    from .local_models import used_for
    return used_for(info.get("pipeline_tag"), info.get("tags") or [], info.get("supports_tools"), info.get("id", ""))


# -- successors --------------------------------------------------------------------
def successors() -> list[dict[str, Any]]:
    wl = CONFIG.watchlist
    exclude = re.compile(wl["exclude"])
    local = CONFIG.registry()
    local_bases = {e.get("base_model") for e in local}
    newest_local: dict[str, str] = {}
    for e in local:
        info = _safe(hf.info, e["base_model"], default={}) if e.get("base_model") else {}
        created = info.get("created")
        fam = e.get("family")
        if fam and created and created > newest_local.get(fam, ""):
            newest_local[fam] = created

    def family_report(fam: dict[str, Any]) -> dict[str, Any]:
        rx = re.compile(fam["match"])
        found = _safe(hf.search, author=fam["author"], sort="createdAt", limit=60, default=[])
        models = [m for m in found if rx.search(m["id"].split("/")[1]) and not exclude.search(m["id"])]
        since = newest_local.get(fam["id"])
        newer = [m for m in models if since and m["created"] and m["created"] > since and m["id"] not in local_bases]
        latest = newer if since else models[:3]
        for m in latest[:6]:
            m["gguf"] = best_gguf_repo(m["id"])
        return {"family": fam["id"], "label": fam["label"], "installed": bool(since),
                "installed_newest": since, "successors": newer[:6] if since else [],
                "latest": [] if since else latest[:3]}

    return list(_POOL.map(family_report, wl["families"]))


# -- popular -------------------------------------------------------------------------
def popular() -> dict[str, Any]:
    jobs = {
        "hf_trending": _POOL.submit(_safe, hf.search, sort="trendingScore", pipeline="text-generation", limit=20, default=[]),
        "hf_trending_gguf": _POOL.submit(_safe, hf.search, sort="trendingScore", gguf=True, limit=20, default=[]),
        "hf_most_downloaded_gguf": _POOL.submit(_safe, hf.search, sort="downloads", gguf=True, limit=20, default=[]),
        "github_rising_llm": _POOL.submit(_safe, github.popular, 90, 15, "llm", 300, default=[]),
        "github_local_llm": _POOL.submit(_safe, github.most_starred, "topic:local-llm stars:>1000", 15, default=[]),
    }
    out = {k: f.result() for k, f in jobs.items()}
    llm_tasks = {None, "text-generation", "image-text-to-text", "any-to-any"}
    for key in ("hf_trending", "hf_trending_gguf", "hf_most_downloaded_gguf"):
        if isinstance(out[key], list):
            out[key] = [_annotate(m) for m in out[key] if m.get("pipeline_tag") in llm_tasks]
    return out


# -- watchlist ------------------------------------------------------------------------
def watchlist() -> list[dict[str, Any]]:
    gpu = system.gpu()

    def one(c: dict[str, Any]) -> dict[str, Any]:
        info = _safe(hf.info, c["base"], default={})
        item = {**c, "id": c["base"], "params": info.get("params"), "created": info.get("created"),
                "downloads": info.get("downloads"), "license": info.get("license"),
                "pipeline_tag": info.get("pipeline_tag"), "url": f"{hf.SITE}/{c['base']}",
                "error": info.get("error")}
        repo = best_gguf_repo(c["base"])
        item["gguf"] = repo
        if repo:
            variants = [v for v in hf.group_gguf(_safe(hf.tree, repo["id"], default=[])) if not v.get("extras")]
            cfg = hf.config_json(c["base"])
            for v in variants:
                v["estimate"] = estimates.estimate(name=f"{c['base']} {v['variant']}", weights_bytes=v["size"],
                                                   total_params=item["params"], cfg=cfg, gpu=gpu)
            rec = recommend(variants, gpu)
            item["best_fit"] = ({"variant": rec["variant"], "quant": rec["quant"], "size_gb": rec["size"] / 1e9,
                                 "estimate": rec["estimate"]} if rec else None)
            item["smallest_gb"] = variants[0]["size"] / 1e9 if variants else None
        return item

    return list(_POOL.map(one, CONFIG.watchlist["candidates"]["models"]))


# -- search ---------------------------------------------------------------------------
def search(query: str, source: str = "huggingface", gguf_only: bool = True) -> list[dict[str, Any]]:
    if source == "modelscope":
        results = modelscope.search(query)
    elif source == "github":
        results = github.most_starred(query)
    else:
        results = hf.search(query, sort="downloads", limit=30, gguf=gguf_only)
    return [_annotate(r) for r in results]
