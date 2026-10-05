"""Background download manager: resumable, host-checked, SHA-256 verified.

A finished file gets a "<file>.verified" marker (same convention as
llm/download-models.ps1). Partial downloads are kept as "<file>.part" and resume.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from . import gguf, sources
from .config import CONFIG
from .net import USER_AGENT, host_allowed

CHUNK = 4 * 1024 * 1024


class DownloadError(Exception):
    pass


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts: list[str]):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not host_allowed(newurl, self.hosts):
            raise DownloadError(f"redirect to non-allow-listed host refused: {urllib.parse.urlsplit(newurl).hostname}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Job:
    def __init__(self, title: str, files: list[dict[str, Any]], hosts: list[str],
                 on_done: Callable[["Job"], None] | None) -> None:
        self.id = uuid.uuid4().hex[:10]
        self.title = title
        self.files = files  # [{url, dest, size, sha256}]
        self.hosts = hosts
        self.on_done = on_done
        self.status = "queued"
        self.error: str | None = None
        self.done_bytes = 0
        self.total_bytes = sum(f.get("size") or 0 for f in files)
        self.speed = 0.0
        self.current = ""
        self.created = time.time()
        self.finished: float | None = None
        self.result: dict[str, Any] | None = None
        self.cancel = threading.Event()

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "status": self.status, "error": self.error,
                "done_bytes": self.done_bytes, "total_bytes": self.total_bytes, "speed_bps": self.speed,
                "current": self.current, "files": [Path(f["dest"]).name for f in self.files],
                "created": self.created, "finished": self.finished, "result": self.result}


class Manager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self._queue: list[Job] = []
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        for i in range(max(1, CONFIG.hub["downloads"]["max_parallel"])):
            threading.Thread(target=self._worker, name=f"download-{i}", daemon=True).start()

    def submit(self, title: str, files: list[dict[str, Any]], hosts: list[str],
               on_done: Callable[[Job], None] | None = None) -> Job:
        allowed = tuple(CONFIG.hub["downloads"]["allowed_extensions"])
        for f in files:
            name = Path(f["dest"]).name
            verdict, why = sources.file_type_verdict(name)
            if verdict != "ok" or not name.lower().endswith(allowed):
                raise DownloadError(f"{name}: {why}")
            if not f.get("sha256") or len(f["sha256"]) != 64:
                raise DownloadError(f"{name}: no SHA-256 checksum available, refusing to download")
            if not host_allowed(f["url"], hosts):
                raise DownloadError(f"{name}: host not allow-listed")
        job = Job(title, files, hosts, on_done)
        with self._cv:
            self.jobs[job.id] = job
            self._queue.append(job)
            self._cv.notify()
        return job

    def cancel(self, job_id: str) -> None:
        job = self.jobs[job_id]
        job.cancel.set()
        with self._cv:
            if job in self._queue:
                self._queue.remove(job)
                job.status = "cancelled"

    def list(self) -> list[dict[str, Any]]:
        return [j.to_dict() for j in sorted(self.jobs.values(), key=lambda j: -j.created)]

    def _worker(self) -> None:
        while True:
            with self._cv:
                while not self._queue:
                    self._cv.wait()
                job = self._queue.pop(0)
            self._run(job)

    def _run(self, job: Job) -> None:
        job.status = "downloading"
        try:
            base_done = 0
            for f in job.files:
                self._fetch(job, f, base_done)
                base_done += f.get("size") or 0
            job.status = "verified"
            if job.on_done:
                job.on_done(job)
        except DownloadError as e:
            job.status, job.error = ("cancelled", None) if job.cancel.is_set() else ("failed", str(e))
        except Exception as e:  # network errors etc.
            job.status, job.error = "failed", f"{type(e).__name__}: {e}"
        job.finished = time.time()

    def _fetch(self, job: Job, f: dict[str, Any], base_done: int) -> None:
        dest = Path(f["dest"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        marker = dest.with_name(dest.name + ".verified")
        job.current = dest.name
        if dest.exists() and marker.exists():
            job.done_bytes = base_done + dest.stat().st_size
            return
        part = dest.with_name(dest.name + ".part")
        have = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": USER_AGENT}
        if have:
            headers["Range"] = f"bytes={have}-"
        opener = urllib.request.build_opener(_CheckedRedirect(job.hosts))
        req = urllib.request.Request(f["url"], headers=headers)
        with opener.open(req, timeout=60) as resp:
            if have and resp.status != 206:
                have = 0  # server ignored the range; start over
            mode = "ab" if have else "wb"
            window_t, window_b = time.time(), 0
            with open(part, mode) as out:
                while True:
                    if job.cancel.is_set():
                        raise DownloadError("cancelled")
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    have += len(chunk)
                    window_b += len(chunk)
                    job.done_bytes = base_done + have
                    now = time.time()
                    if now - window_t >= 2:
                        job.speed = window_b / (now - window_t)
                        window_t, window_b = now, 0
        expected = f.get("size")
        if expected and have != expected:
            raise DownloadError(f"{dest.name}: size {have} != expected {expected} (re-run to resume)")
        job.current = f"verifying {dest.name}"
        digest = _sha256(part)
        if digest != f["sha256"].lower():
            bad = part.with_name(part.name + ".bad")
            os.replace(part, bad)
            raise DownloadError(f"{dest.name}: SHA-256 mismatch (kept as {bad.name})")
        if dest.suffix.lower() == ".gguf" and not gguf.is_gguf(part):
            raise DownloadError(f"{dest.name}: not a GGUF file")
        os.replace(part, dest)
        marker.write_text(digest + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


MANAGER: Manager | None = None


def manager() -> Manager:
    global MANAGER
    if MANAGER is None:
        MANAGER = Manager()
    return MANAGER
