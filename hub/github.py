"""GitHub client: popular LLM repositories and release assets (with SHA-256 digests).

Unauthenticated GitHub API calls are limited (60/hour, 10 searches/minute), so
results are cached for an hour. Set GITHUB_TOKEN to raise the limit.
"""

from __future__ import annotations

import datetime as dt
import os
import urllib.parse
from typing import Any

from . import net
from .config import CONFIG

API = "https://api.github.com"


def _headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _ttl() -> int:
    return CONFIG.hub["cache_ttl_seconds"]["github"]


def _repo(r: dict[str, Any]) -> dict[str, Any]:
    return {"id": r["full_name"], "description": r.get("description"), "stars": r.get("stargazers_count"),
            "forks": r.get("forks_count"), "updated": r.get("pushed_at"), "created": r.get("created_at"),
            "topics": r.get("topics") or [], "license": (r.get("license") or {}).get("spdx_id"),
            "url": r.get("html_url"), "source": "github"}


def popular(days: int = 90, limit: int = 15, topic: str = "llm", min_stars: int = 300) -> list[dict[str, Any]]:
    """Rising repos: most-starred on a topic, created in the last ``days`` days."""
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    q = urllib.parse.quote(f"topic:{topic} created:>{since} stars:>{min_stars}")
    data = net.get_json(f"{API}/search/repositories?q={q}&sort=stars&order=desc&per_page={limit}",
                        ttl=_ttl(), headers=_headers())
    return [_repo(r) for r in data.get("items", [])]


def most_starred(query: str, limit: int = 15) -> list[dict[str, Any]]:
    q = urllib.parse.quote(query)
    data = net.get_json(f"{API}/search/repositories?q={q}&sort=stars&order=desc&per_page={limit}",
                        ttl=_ttl(), headers=_headers())
    return [_repo(r) for r in data.get("items", [])]


def releases(repo: str, limit: int = 5) -> list[dict[str, Any]]:
    data = net.get_json(f"{API}/repos/{repo}/releases?per_page={limit}", ttl=_ttl(), headers=_headers())
    out = []
    for rel in data:
        out.append({
            "tag": rel.get("tag_name"), "name": rel.get("name"), "published": rel.get("published_at"),
            "prerelease": rel.get("prerelease"), "url": rel.get("html_url"),
            "assets": [{"name": a["name"], "size": a["size"], "url": a["browser_download_url"],
                        "sha256": (a.get("digest") or "").removeprefix("sha256:") or None,
                        "downloads": a.get("download_count")} for a in rel.get("assets", [])],
        })
    return out
