"""Resource monitor with user-chosen modes:

  continuous - sample until stopped
  duration   - sample for a set time, then stop
  average    - sample for a set time, then stop and report the average (and min/peak)
  stopped    - no sampling at all (the hub does no background work)

Samples CPU %, Windows RAM used and GPU memory used (dedicated + shared, from the
Windows GPU counters). Each sample is ~1 ms of work; the default interval is 2 s.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

from . import pdh, system

MAX_SAMPLES = 7200   # 4 h at 2 s
MIN_INTERVAL, MAX_INTERVAL = 1.0, 60.0
MAX_DURATION = 24 * 3600

METRICS = ("cpu_percent", "ram_used_gb", "gpu_used_gb")


class Monitor:
    def __init__(self) -> None:
        self.mode = "stopped"
        self.interval = 2.0
        self.duration: float | None = None
        self.started: float | None = None
        self.ended: float | None = None
        self.samples: deque[dict[str, float]] = deque(maxlen=MAX_SAMPLES)
        self._cpu = system._CpuSampler()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- control ------------------------------------------------------------
    def start(self, mode: str, seconds: float | None = None, interval: float | None = None) -> dict[str, Any]:
        if mode not in ("continuous", "duration", "average"):
            raise ValueError("mode must be continuous, duration or average")
        if mode != "continuous":
            if not seconds or not 5 <= seconds <= MAX_DURATION:
                raise ValueError(f"duration must be between 5 seconds and {MAX_DURATION // 3600} hours")
        interval = float(interval or self.interval)
        if not MIN_INTERVAL <= interval <= MAX_INTERVAL:
            raise ValueError(f"interval must be {MIN_INTERVAL:g}-{MAX_INTERVAL:g} s")
        self.stop(reason=None)
        with self._lock:
            self.mode, self.interval = mode, interval
            self.duration = float(seconds) if mode != "continuous" else None
            self.started, self.ended = time.time(), None
            self.samples.clear()
            self._stop.clear()
            self._cpu.sample()  # prime the CPU delta
            self._thread = threading.Thread(target=self._run, name="monitor", daemon=True)
            self._thread.start()
        return self.status()

    def stop(self, reason: str | None = "stopped by user") -> dict[str, Any]:
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5)
        with self._lock:
            if self.started and not self.ended:
                self.ended = time.time()
            if reason:
                self.stop_reason = reason
            self._thread = None
        return self.status()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self._sample()
            if self.duration and time.time() - (self.started or 0) >= self.duration:
                with self._lock:
                    self.ended = time.time()
                    self.stop_reason = "duration reached"
                    self._thread = None
                return

    def _sample(self) -> None:
        mem = system.memory()
        gpu_used = None
        usage = pdh.gpu_memory_usage(max_age=0)
        if usage:
            from . import hardware
            luid = ((hardware.static_info().get("primary") or {}).get("luid") or "").lower()
            u = next((v for k, v in usage.items() if k.lower() == luid), None)
            if u:
                gpu_used = (u.get("dedicated_gb") or 0) + (u.get("shared_gb") or 0)
        self.samples.append({"t": time.time(), "cpu_percent": self._cpu.sample(),
                             "ram_used_gb": mem["used_gb"], "gpu_used_gb": gpu_used})

    # -- reporting ------------------------------------------------------------
    def status(self, since: float | None = None) -> dict[str, Any]:
        with self._lock:
            samples = list(self.samples)
            running = self._thread is not None and self._thread.is_alive()
            mode, started, ended, duration = self.mode, self.started, self.ended, self.duration
        stats = {}
        for m in METRICS:
            vals = [s[m] for s in samples if s.get(m) is not None]
            if vals:
                stats[m] = {"avg": sum(vals) / len(vals), "min": min(vals), "max": max(vals), "last": vals[-1]}
        now = time.time()
        return {
            "mode": mode, "running": running, "interval": self.interval, "duration": duration,
            "started": started, "ended": ended,
            "elapsed": ((ended or now) - started) if started else 0,
            "remaining": max(0.0, duration - (now - started)) if (running and duration and started) else None,
            "stop_reason": getattr(self, "stop_reason", None) if not running else None,
            "count": len(samples), "stats": stats,
            "samples": [s for s in samples if since is None or s["t"] > since][-600:],
        }


MONITOR = Monitor()
