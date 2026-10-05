"""ModelScope (modelscope.cn) client: search and file listings with SHA-256."""

from __future__ import annotations

import urllib.parse
from typing import Any

from . import net
from .config import CONFIG

SITE = "https://www.modelscope.cn"


def _ttl() -> int:
    return CONFIG.hub["cache_ttl_seconds"]["huggingface"]


def search(query: str, limit: int = 20) -> list[dict[str, Any]]:
    body = {"PageSize": limit, "PageNumber": 1, "SortBy": "Default", "Target": "", "Criterion": [], "Name": query}
    data = net.get_json(f"{SITE}/api/v1/dolphin/models", ttl=_ttl(), method="PUT", body=body)
    out = []
    for m in ((data.get("Data") or {}).get("Model") or {}).get("Models") or []:
        repo = f"{m.get('Path')}/{m.get('Name')}"
        out.append({"id": repo, "author": m.get("Path"), "downloads": m.get("Downloads"), "likes": m.get("Stars"),
                    "created": m.get("CreatedTime"), "license": m.get("License"),
                    "pipeline_tag": ((m.get("Tasks") or [{}])[0] or {}).get("Name"),
                    "url": f"{SITE}/models/{repo}", "source": "modelscope"})
    return out


def tree(repo: str) -> list[dict[str, Any]]:
    data = net.get_json(f"{SITE}/api/v1/models/{repo}/repo/files?Revision=master&Recursive=true", ttl=_ttl())
    return [{"path": f["Path"], "size": f.get("Size") or 0, "sha256": f.get("Sha256")}
            for f in ((data.get("Data") or {}).get("Files") or []) if f.get("Type") == "blob"]


def file_url(repo: str, path: str) -> str:
    return f"{SITE}/models/{repo}/resolve/master/{urllib.parse.quote(path)}"
