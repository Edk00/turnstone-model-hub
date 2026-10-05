"""Model source registry and safety checks.

A source must pass these checks before Model Hub will download from it:
  * HTTPS with a valid certificate (Python's default TLS verification).
  * Every DNS address for the host is public (blocks LAN/loopback/metadata IPs).
  * Redirects stay on HTTPS and on allow-listed hosts.
  * Only weight-only formats (.gguf, .safetensors); pickle-based (.bin/.pt/.pth/.ckpt/.pkl)
    and executable/archive types are refused, because they can run code when loaded.
  * A SHA-256 checksum must be available (from the registry, or supplied by you) and
    is verified after download; GGUF files must also start with the GGUF magic bytes.
See docs/SECURITY.md.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import CONFIG
from .net import USER_AGENT, host_allowed

SAFE_EXT = (".gguf", ".safetensors")
PICKLE_EXT = (".bin", ".pt", ".pth", ".ckpt", ".pkl", ".pickle", ".joblib", ".npy", ".npz")
EXEC_EXT = (".exe", ".dll", ".msi", ".bat", ".cmd", ".ps1", ".sh", ".py", ".js", ".jar", ".scr",
            ".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".xz", ".appimage", ".deb", ".rpm", ".dmg")


def file_type_verdict(name: str) -> tuple[str, str]:
    n = name.lower()
    if n.endswith(SAFE_EXT):
        return "ok", "weights-only format (no executable code)"
    if n.endswith(PICKLE_EXT):
        return "blocked", "pickle-based format can execute code when loaded"
    if n.endswith(EXEC_EXT):
        return "blocked", "executable or archive file"
    return "blocked", "unrecognised file type"


def publisher_tier(owner: str) -> str:
    tp = CONFIG.sources_doc.get("trusted_publishers", {})
    if owner in tp.get("official", []):
        return "official"
    if owner in tp.get("quantizers", []):
        return "known quantizer"
    return "community"


def source_for_url(url: str) -> dict[str, Any] | None:
    for s in CONFIG.sources():
        if s.get("trust") != "blocked" and host_allowed(url, s["hosts"]):
            return s
    return None


def _public_host(host: str) -> tuple[bool, list[str]]:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False, []
    addrs = sorted({i[4][0] for i in infos})
    ok = bool(addrs) and all(ipaddress.ip_address(a).is_global for a in addrs)
    return ok, addrs


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _head(url: str) -> tuple[int, dict[str, str]]:
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(req, timeout=20) as r:
            return r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {})


def check_url(url: str, extra_hosts: list[str] | None = None) -> dict[str, Any]:
    """Run the safety checks on a source or file URL and explain the verdict."""
    checks: list[dict[str, Any]] = []
    add = lambda name, ok, detail: checks.append({"check": name, "ok": ok, "detail": detail})  # noqa: E731
    parts = urllib.parse.urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    add("HTTPS", parts.scheme == "https", parts.scheme or "no scheme")
    public, addrs = _public_host(host) if host else (False, [])
    add("Public internet address", public, ", ".join(addrs) or "host does not resolve")

    known = source_for_url(url)
    add("Known model registry", bool(known), known["name"] if known else "not in sources.json")

    hops, redirect_ok, status, headers = [], True, None, {}
    tls_ok = None
    if parts.scheme == "https" and public:
        allowed = (known["hosts"] if known else [host]) + (extra_hosts or [])
        current = url
        try:
            for _ in range(6):
                status, headers = _head(current)
                hops.append({"url": current.split("?")[0], "status": status})
                if status in (301, 302, 303, 307, 308) and headers.get("Location"):
                    nxt = urllib.parse.urljoin(current, headers["Location"])
                    nh = urllib.parse.urlsplit(nxt).hostname or ""
                    if not host_allowed(nxt, allowed) or not _public_host(nh)[0]:
                        redirect_ok = False
                        hops.append({"url": nxt.split("?")[0], "status": "blocked: host not allow-listed"})
                        break
                    current = nxt
                    continue
                break
            tls_ok = True
        except ssl.SSLError as e:
            tls_ok = False
            hops.append({"url": url, "status": f"TLS error: {e}"})
        except (urllib.error.URLError, OSError) as e:
            tls_ok = "CERTIFICATE" not in str(e).upper()
            hops.append({"url": url, "status": f"error: {e}"})
    add("Valid TLS certificate", bool(tls_ok), "verified" if tls_ok else "failed or not checked")
    add("Redirects stay on allowed HTTPS hosts", redirect_ok, f"{len(hops)} hop(s)")

    path = parts.path.rsplit("/", 1)[-1]
    is_file = "." in path and not path.endswith((".html", ".htm"))
    if is_file:
        verdict, why = file_type_verdict(path)
        add("Safe file type", verdict == "ok", why)
        size = headers.get("Content-Length") or headers.get("X-Linked-Size")
        etag = (headers.get("X-Linked-ETag") or "").strip('"')
        has_sum = bool(known) or len(etag) == 64
        add("Checksum available", has_sum,
            "from registry metadata" if known else ("SHA-256 in X-Linked-ETag" if len(etag) == 64 else "you must supply a SHA-256"))
    else:
        size = None

    failed = [c for c in checks if not c["ok"]]
    hard = {"HTTPS", "Public internet address", "Valid TLS certificate", "Redirects stay on allowed HTTPS hosts", "Safe file type"}
    if any(c["check"] in hard for c in failed):
        verdict = "unsafe"
    elif failed:
        verdict = "caution"
    else:
        verdict = "safe"
    return {"url": url, "host": host, "verdict": verdict, "checks": checks, "redirects": hops,
            "size": int(size) if size and str(size).isdigit() else None, "known_source": known and known["id"]}


def add_source(url: str, name: str) -> dict[str, Any]:
    """Add a reviewed source after it passes the safety checks."""
    result = check_url(url)
    if result["verdict"] == "unsafe":
        raise ValueError("source failed safety checks: " +
                         "; ".join(c["check"] for c in result["checks"] if not c["ok"]))
    host = result["host"]
    sid = host.replace(".", "-")
    if any(s["id"] == sid for s in CONFIG.sources()):
        raise ValueError(f"source {host} already exists")
    entry = {"id": sid, "name": name or host, "kind": "direct", "base_url": f"https://{host}",
             "hosts": [host], "trust": "reviewed",
             "checksum": "you must supply a SHA-256 for each file", "checked": result["checks"]}
    CONFIG.sources().append(entry)
    CONFIG.save_sources()
    return entry


def set_trust(source_id: str, trust: str) -> dict[str, Any]:
    if trust not in ("trusted", "reviewed", "blocked"):
        raise ValueError("trust must be trusted, reviewed or blocked")
    for s in CONFIG.sources():
        if s["id"] == source_id:
            s["trust"] = trust
            CONFIG.save_sources()
            return s
    raise KeyError(source_id)
