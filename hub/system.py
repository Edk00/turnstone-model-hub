"""System resources: CPU, RAM, GPU memory, disk, Turnstone and model-server status.

Windows is the primary target (any GPU + llama.cpp); Linux falls back to
/proc. GPU memory comes from `llama-server --list-devices`, which reports what the
inference backend can actually allocate (dedicated + shared), plus the dedicated
size from the Windows display-adapter registry key.
"""

from __future__ import annotations

import ctypes
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from . import net
from .config import CONFIG

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

_GB = 1024 ** 3


def _run(cmd: list[str], timeout: float = 20) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
        raw = out.stdout + out.stderr
    except (OSError, subprocess.TimeoutExpired):
        return ""
    # wsl.exe prints UTF-16LE; everything else here is UTF-8.
    if raw[:2] == b"\xff\xfe" or (len(raw) > 1 and raw[1:2] == b"\x00"):
        return raw.decode("utf-16-le", "replace")
    return raw.decode("utf-8", "replace")


# -- CPU ---------------------------------------------------------------------
class _CpuSampler:
    """CPU utilisation from GetSystemTimes deltas (Windows) or /proc/stat."""

    def __init__(self) -> None:
        self._last: tuple[int, int] | None = None
        self.percent = 0.0

    def _times(self) -> tuple[int, int] | None:
        if IS_WINDOWS:
            idle, kernel, user = (ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong())
            if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
                return None
            return idle.value, kernel.value + user.value  # kernel time includes idle
        try:
            with open("/proc/stat") as f:
                vals = [int(v) for v in f.readline().split()[1:]]
            return vals[3] + vals[4], sum(vals)
        except OSError:
            return None

    def sample(self) -> float:
        t = self._times()
        if t and self._last:
            d_idle, d_total = t[0] - self._last[0], t[1] - self._last[1]
            if d_total > 0:
                self.percent = round(100.0 * (1 - d_idle / d_total), 1)
        self._last = t
        return self.percent


_cpu = _CpuSampler()


def cpu_name() -> str:
    if IS_WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine()


# -- RAM ---------------------------------------------------------------------
def memory() -> dict[str, float]:
    if IS_WINDOWS:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(m)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        total, avail = m.ullTotalPhys, m.ullAvailPhys
    else:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) * 1024
        total, avail = info["MemTotal"], info.get("MemAvailable", info["MemFree"])
    return {"total_gb": total / _GB, "used_gb": (total - avail) / _GB, "available_gb": avail / _GB}


def installed_memory_gb() -> float | None:
    """Physical RAM installed, including any carved out for the iGPU."""
    if IS_WINDOWS:
        kb = ctypes.c_ulonglong()
        if ctypes.windll.kernel32.GetPhysicallyInstalledSystemMemory(ctypes.byref(kb)):
            return kb.value / 1024 / 1024
    return None


# -- GPU ---------------------------------------------------------------------
def _gpu_registry() -> dict[str, Any]:
    """Name and dedicated memory of the first display adapter with memory info."""
    if not IS_WINDOWS:
        return {}
    import winreg
    base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    for i in range(16):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{base}\\{i:04d}") as k:
                size = winreg.QueryValueEx(k, "HardwareInformation.qwMemorySize")[0]
                name = winreg.QueryValueEx(k, "DriverDesc")[0]
                return {"name": name, "dedicated_gb": int(size) / _GB}
        except OSError:
            continue
    return {}


_DEVICE_RE = re.compile(r"^\s*(\S+): (.+?) \((\d+) MiB, (\d+) MiB free\)", re.M)


def _llama_devices() -> list[dict[str, Any]]:
    exe = CONFIG.llama_server
    if not exe.exists():
        return []
    out = _run([str(exe), "--list-devices"], timeout=30)
    return [{"id": m[0], "name": m[1], "total_gb": int(m[2]) / 1024, "free_gb": int(m[3]) / 1024}
            for m in _DEVICE_RE.findall(out)]


# -- Turnstone / model servers -----------------------------------------------
_timed: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: float, fn: Any) -> Any:
    hit = _timed.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = fn()
    _timed[key] = (time.time(), value)
    return value


def listening_ports() -> set[int]:
    """Local TCP ports in LISTEN state. Probing closed ports is slow on Windows
    (~2 s per refused connection), so only listening ports are queried."""
    def read() -> set[int]:
        out = _run(["netstat", "-an", "-p", "tcp"] if IS_WINDOWS else ["ss", "-ltn"], timeout=10)
        ports = set()
        for line in out.splitlines():
            if "LISTEN" in line.upper():
                for tok in line.split():
                    if ":" in tok:
                        tail = tok.rsplit(":", 1)[1]
                        if tail.isdigit():
                            ports.add(int(tail))
                            break
        return ports
    return _cached("ports", 2, read)


def wsl_running(distro: str) -> bool | None:
    if not IS_WINDOWS:
        return None
    out = _cached("wsl", 10, lambda: _run(["wsl.exe", "-l", "--running", "-q"], timeout=10))
    return distro.lower() in out.replace("\x00", "").lower()


def turnstone_status() -> dict[str, Any]:
    ts = CONFIG.hub["turnstone"]
    status: dict[str, Any] = {"console_url": ts["console_url"], "wsl_running": wsl_running(ts["wsl_distro"]), "up": False}
    port = urllib.parse.urlsplit(ts["console_url"]).port or 80
    if port in listening_ports():
        try:
            h = net.get_json(ts["console_url"].rstrip("/") + "/health", timeout=3)
            status.update(up=True, nodes=h.get("nodes"), versions=h.get("versions"), workstreams=h.get("workstreams"))
        except net.FetchError:
            pass
    nodes = status.get("nodes") or 0
    if nodes:
        global last_turnstone_nodes
        last_turnstone_nodes = nodes
    status["node_memory_reserved_gb"] = nodes * ts["node_memory_limit_gb"]
    return status


last_turnstone_nodes: int | None = None  # seen on the console; overrides hub.json 'nodes'


def model_servers() -> dict[str, dict[str, Any]]:
    """Status of every llama-server in the registry, keyed by model name."""
    result = {}
    ports = listening_ports()
    for m in CONFIG.registry():
        info: dict[str, Any] = {"port": m["port"], "running": False}
        if m["port"] not in ports:
            result[m["name"]] = info
            continue
        try:
            h = net.get_json(f"http://127.0.0.1:{m['port']}/health", timeout=1.5)
            info["running"] = h.get("status") == "ok"
            info["state"] = h.get("status")
        except net.FetchError as e:
            if "503" in str(e):  # still loading
                info["state"] = "loading"
        result[m["name"]] = info
    return result


def folder_size_gb(path: Path) -> float:
    total = 0
    if path.exists():
        for p in path.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
    return total / _GB


# -- GPU view used by the dashboard and the estimator ------------------------
def gpu(mem: dict[str, float] | None = None) -> dict[str, Any]:
    """GPU memory capacity, usage and the budget models may use (see hardware.py).
    Detection is cheap (DXGI once + performance counters per call), so nothing
    polls in the background."""
    from . import hardware
    b = hardware.budget(mem)
    used = None
    if b["used_dedicated_gb"] is not None:
        used = b["used_dedicated_gb"] + (b["used_shared_gb"] or 0 if b["kind"] == "integrated" else 0)
    usable = b["dedicated_gb"] + (b["shared_limit_gb"] if b["kind"] == "integrated" else 0)
    return {**b, "usable_total_gb": usable, "used_gb": used,
            "free_gb": (usable - used) if used is not None else None,
            "shared_gb": b["shared_limit_gb"] if b["kind"] == "integrated" else 0}


def snapshot() -> dict[str, Any]:
    disk = shutil.disk_usage(CONFIG.models_dir if CONFIG.models_dir.exists() else Path.cwd())
    mem = memory()
    return {
        "time": time.time(),
        "host": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "cpu": {"name": cpu_name(), "cores": os.cpu_count(), "percent": _cpu.sample()},
        "memory": {**mem, "installed_gb": installed_memory_gb(), "source": "detected (Windows)"},
        "gpu": gpu(mem),
        "disk": {"path": str(CONFIG.models_dir), "total_gb": disk.total / _GB,
                 "used_gb": disk.used / _GB, "available_gb": disk.free / _GB,
                 "models_gb": folder_size_gb(CONFIG.models_dir)},
        "turnstone": turnstone_status(),
        "servers": model_servers(),
    }


def start_background_refresh() -> None:
    """Detect static hardware once at startup (off the request path)."""
    from . import hardware
    threading.Thread(target=hardware.static_info, name="hw-detect", daemon=True).start()
