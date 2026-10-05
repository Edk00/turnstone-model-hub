"""Hugging Face Hub client (public REST API, no token required for public repos)."""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from . import net
from .config import CONFIG

API = "https://huggingface.co/api"
SITE = "https://huggingface.co"
_EXPAND = ["gguf", "pipeline_tag", "tags", "downloads", "likes", "createdAt", "lastModified",
           "gated", "baseModels", "cardData", "trendingScore", "safetensors", "library_name"]
_LIST_EXPAND = ["pipeline_tag", "tags", "downloads", "likes", "createdAt", "lastModified",
                "gated", "trendingScore", "safetensors"]


def _ttl() -> int:
    return CONFIG.hub["cache_ttl_seconds"]["huggingface"]


def _q(params: list[tuple[str, Any]]) -> str:
    return urllib.parse.urlencode([(k, v) for k, v in params if v not in (None, "")])


def _summary(m: dict[str, Any]) -> dict[str, Any]:
    st = m.get("safetensors") or {}
    return {
        "id": m.get("id") or m.get("modelId"),
        "author": (m.get("id") or "").split("/")[0],
        "downloads": m.get("downloads"),
        "likes": m.get("likes"),
        "trending": m.get("trendingScore"),
        "created": m.get("createdAt"),
        "updated": m.get("lastModified"),
        "pipeline_tag": m.get("pipeline_tag"),
        "tags": m.get("tags") or [],
        "gated": bool(m.get("gated")),
        "params": st.get("total"),
        "url": f"{SITE}/{m.get('id')}",
        "source": "huggingface",
    }


def search(query: str = "", *, author: str | None = None, sort: str = "downloads",
           limit: int = 30, gguf: bool = False, pipeline: str | None = None,
           extra_filters: list[str] | None = None) -> list[dict[str, Any]]:
    params: list[tuple[str, Any]] = [("search", query), ("author", author), ("sort", sort),
                                     ("direction", "-1"), ("limit", min(limit, 100))]
    if gguf:
        params.append(("filter", "gguf"))
    if pipeline:
        params.append(("pipeline_tag", pipeline))
    for f in extra_filters or []:
        params.append(("filter", f))
    params += [("expand[]", e) for e in _LIST_EXPAND]
    return [_summary(m) for m in net.get_json(f"{API}/models?{_q(params)}", ttl=_ttl())]


def quantized_of(base: str, limit: int = 10) -> list[dict[str, Any]]:
    """GGUF repos that declare ``base`` as their quantization source."""
    return search(sort="downloads", limit=limit, gguf=True, extra_filters=[f"base_model:quantized:{base}"])


def info(repo: str) -> dict[str, Any]:
    params = _q([("expand[]", e) for e in _EXPAND])
    m = net.get_json(f"{API}/models/{repo}?{params}", ttl=_ttl())
    s = _summary(m)
    gguf = m.get("gguf") or {}
    card = m.get("cardData") or {}
    base = m.get("baseModels") or {}
    template = gguf.get("chat_template") or ""
    s.update(
        library=m.get("library_name"),
        license=card.get("license") or (base.get("models") or [{}])[0].get("license") if isinstance(base, dict) else card.get("license"),
        base_models=[b.get("id") for b in (base.get("models") or [])] if isinstance(base, dict) else [],
        gguf_params=gguf.get("total"),
        architecture=gguf.get("architecture"),
        context_length=gguf.get("context_length"),
        supports_tools=("tools" in template) if template else None,
        has_vision=None,
    )
    if not s["params"]:
        s["params"] = gguf.get("total")
    return s


def config_json(repo: str) -> dict[str, Any] | None:
    """The model's config.json (architecture details for memory estimates)."""
    try:
        cfg = net.get_json(f"{SITE}/{repo}/resolve/main/config.json", ttl=_ttl())
    except net.FetchError:
        return None
    return cfg if isinstance(cfg, dict) else None


def tree(repo: str) -> list[dict[str, Any]]:
    entries = net.get_json(f"{API}/models/{repo}/tree/main?recursive=true", ttl=_ttl())
    out = []
    for e in entries:
        if e.get("type") != "file":
            continue
        lfs = e.get("lfs") or {}
        out.append({"path": e["path"], "size": lfs.get("size") or e.get("size") or 0,
                    "sha256": lfs.get("oid") if lfs else None})
    return out


def file_url(repo: str, path: str) -> str:
    return f"{SITE}/{repo}/resolve/main/{urllib.parse.quote(path)}"


# -- GGUF file grouping --------------------------------------------------------
_SPLIT = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.I)
_QUANT = re.compile(
    r"(?i)(?:^|[-_.])((?:UD-)?(?:IQ\d_[A-Z0-9]+|Q\d_K(?:_[A-Z]+)?|Q\d_\d|Q\d_K|TQ\d_\d|MXFP4(?:_MOE)?|NVFP4|BF16|F16|F32|Q8_0))(?=[-_.])")
_EXTRA = re.compile(r"(?i)(^|[-_/])(mmproj|mtp|dflash|eagle\d?|imatrix|draft)(?=[-_.]|$)")


def group_gguf(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group GGUF files into downloadable variants (split parts together)."""
    groups: dict[str, dict[str, Any]] = {}
    extras = []
    for f in files:
        path = f["path"]
        if not path.lower().endswith(".gguf"):
            continue
        name = path.rsplit("/", 1)[-1]
        extra = _EXTRA.search(name)
        if extra:
            kind = extra.group(2).lower()
            extras.append({**f, "kind": "vision projector" if kind == "mmproj" else kind})
            continue
        key = _SPLIT.sub(".gguf", path)
        q = _QUANT.search(name)
        g = groups.setdefault(key, {"variant": key.rsplit("/", 1)[-1][:-5], "quant": q.group(1).upper() if q else "?",
                                    "files": [], "size": 0})
        g["files"].append(f)
        g["size"] += f["size"]
    for g in groups.values():
        g["files"].sort(key=lambda f: f["path"])
        g["first_file"] = g["files"][0]["path"]
        g["parts"] = len(g["files"])
    return sorted(groups.values(), key=lambda g: g["size"]) + ([{"variant": "extras", "quant": "-", "files": extras,
                                                                 "size": sum(e["size"] for e in extras), "extras": True}]
                                                               if extras else [])
