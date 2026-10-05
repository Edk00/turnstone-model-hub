"""Small HTTP helpers: JSON GET with a TTL cache, host allow-listing."""

from __future__ import annotations

import fnmatch
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import __version__

USER_AGENT = f"turnstone-model-hub/{__version__}"

_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


class FetchError(Exception):
    pass


def get_json(url: str, ttl: float = 0, headers: dict[str, str] | None = None,
             method: str = "GET", body: Any = None, timeout: float = 20) -> Any:
    """Fetch JSON. Responses for GET requests are cached for ``ttl`` seconds."""
    key = f"{method} {url} {json.dumps(body, sort_keys=True) if body is not None else ''}"
    if ttl:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json",
        **({"Content-Type": "application/json"} if data else {}), **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            value = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read(300).decode("utf-8", "replace")
        raise FetchError(f"{e.code} from {urllib.parse.urlsplit(url).netloc}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError(f"{urllib.parse.urlsplit(url).netloc}: {e}") from e
    if ttl:
        with _cache_lock:
            _cache[key] = (time.time(), value)
    return value


def host_allowed(url: str, patterns: list[str]) -> bool:
    """True if the URL is https and its host matches one of the patterns ("*.hf.co")."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return False
    host = parts.hostname.lower()
    return any(host == p or fnmatch.fnmatch(host, p) for p in patterns)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
