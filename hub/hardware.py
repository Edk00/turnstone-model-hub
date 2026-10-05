"""Hardware detection: GPU type and memory, system RAM, memory bandwidth, and the
memory budget a model may use. Every value carries a 'source' so the dashboard can
show whether it was detected, derived, configured or assumed.

GPU memory model (see docs/ASSUMPTIONS.md):
  integrated / unified (AMD APU, Intel iGPU, Apple-like UMA):
      GPU can use its dedicated carve-out + up to the shared limit of system RAM,
      at the same speed. Shared memory competes with Windows, WSL and Turnstone.
  discrete (separate VRAM):
      GPU runs from VRAM. A model larger than VRAM can still run with some layers
      in system RAM on the CPU ("partial offload"), much more slowly.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from . import dxgi, pdh
from .config import CONFIG

_GB = 1024 ** 3
_lock = threading.Lock()
_static: dict[str, Any] | None = None


def _detect_ram_modules() -> list[dict[str, Any]]:
    """Physical memory modules (speed, data width, form factor) via WMI."""
    from .system import _run
    if os.name != "nt":
        return []
    out = _run(["powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity,ConfiguredClockSpeed,Speed,"
                "DataWidth,FormFactor,SMBIOSMemoryType | ConvertTo-Json -Compress"], timeout=30)
    try:
        data = json.loads(out.strip() or "[]")
    except ValueError:
        return []
    return data if isinstance(data, list) else [data]


def ram_bandwidth(modules: list[dict[str, Any]]) -> dict[str, Any]:
    """Peak system-memory bandwidth from module speed and bus width.

    Soldered LPDDR (form factor 0/unknown, 16/32-bit modules) sums module widths.
    Socketed DIMM/SODIMM systems are assumed dual-channel (2 x 64-bit) however many
    sticks are installed, because extra DIMMs share channels."""
    if not modules:
        return {"peak_gbps": None, "source": "unknown"}
    speed = max((m.get("ConfiguredClockSpeed") or m.get("Speed") or 0) for m in modules)
    widths = [m.get("DataWidth") or 64 for m in modules]
    socketed = any(m.get("FormFactor") in (8, 12) for m in modules)
    bus_bits = 128 if socketed else sum(widths)
    peak = speed * 1e6 * bus_bits / 8 / 1e9 if speed else None
    return {"peak_gbps": peak, "mt_s": speed, "bus_bits": bus_bits, "modules": len(modules),
            "layout": "socketed (assumed dual-channel)" if socketed else "soldered (summed module widths)",
            "source": "detected (WMI)"}


def static_info(force: bool = False) -> dict[str, Any]:
    """Slow-changing facts, detected once (or on Refresh)."""
    global _static
    with _lock:
        if _static is not None and not force:
            return _static
    from . import system
    mem = system.memory()
    installed = system.installed_memory_gb()
    adapters = [a for a in dxgi.adapters() if a["vendor"] != "Microsoft"]
    if not adapters:  # DXGI unavailable: fall back to the display-adapter registry key
        reg = system._gpu_registry()
        if reg:
            adapters = [{"name": reg["name"], "vendor": "unknown", "dedicated_gb": reg["dedicated_gb"],
                         "dedicated_system_gb": 0.0, "shared_limit_gb": mem["total_gb"] / 2, "luid": None,
                         "source": "registry (shared limit assumed 50% of RAM)"}]
    override = CONFIG.hub["hardware"].get("gpu_kind_override")
    for a in adapters:
        a["kind"] = override or dxgi.classify(a, installed, mem["total_gb"])
    backend = system._llama_devices()
    primary = None
    if backend and adapters:  # the adapter llama.cpp actually uses
        primary = next((a for a in adapters if a["name"].lower() in backend[0]["name"].lower()
                        or backend[0]["name"].lower() in a["name"].lower()), None)
    if primary is None and adapters:
        primary = max(adapters, key=lambda a: a["dedicated_gb"])
    bw = ram_bandwidth(_detect_ram_modules())
    info = {"adapters": adapters, "primary": primary, "backend_devices": backend,
            "installed_ram_gb": installed, "ram_bandwidth": bw, "detected_at": time.time()}
    with _lock:
        _static = info
    return info


def effective_bandwidth(info: dict[str, Any]) -> dict[str, Any]:
    """Bandwidth used for speed estimates, with its source."""
    hw = CONFIG.hub["hardware"]
    if hw.get("effective_bandwidth_gbps"):
        return {"gbps": hw["effective_bandwidth_gbps"], "source": "configured (hub.json)"}
    eff = hw.get("bandwidth_efficiency", 0.45)
    kind = (info.get("primary") or {}).get("kind")
    peak = info["ram_bandwidth"].get("peak_gbps")
    if kind == "integrated" and peak:
        return {"gbps": peak * eff, "source": f"derived: {peak:.0f} GB/s RAM peak x {eff:.0%} efficiency"}
    if kind == "discrete":
        v = hw.get("discrete_vram_bandwidth_gbps")
        if v:
            return {"gbps": v * eff, "source": f"configured VRAM {v} GB/s x {eff:.0%}"}
        return {"gbps": 400 * eff, "source": "assumed 400 GB/s VRAM (set discrete_vram_bandwidth_gbps); benchmark to calibrate"}
    if peak:
        return {"gbps": peak * eff, "source": f"derived from RAM peak {peak:.0f} GB/s (CPU inference)"}
    return {"gbps": 60.0, "source": "assumed (no detection)"}


def cpu_bandwidth(info: dict[str, Any]) -> float:
    hw = CONFIG.hub["hardware"]
    peak = info["ram_bandwidth"].get("peak_gbps") or 80
    return peak * hw.get("bandwidth_efficiency", 0.45)


def turnstone_stack_gb() -> float:
    """RAM the Turnstone Docker stack may take (nodes x node limit + ~4 GB for the rest)."""
    from . import system
    ts = CONFIG.hub["turnstone"]
    if ts.get("stack_memory_gb") is not None:
        return float(ts["stack_memory_gb"])
    nodes = system.last_turnstone_nodes or ts.get("nodes", 1)
    return nodes * ts["node_memory_limit_gb"] + 4


def split(info: dict[str, Any] | None = None, mem: dict[str, float] | None = None) -> dict[str, Any]:
    """How the physical RAM is divided on a unified-memory machine (see docs/ASSUMPTIONS.md,
    'RAM segregation'): firmware GPU reservation, RAM Windows sees, and the share of that RAM
    Windows lets the GPU borrow."""
    from . import system
    info = info or static_info()
    mem = mem or system.memory()
    p = info.get("primary") or {}
    installed = info.get("installed_ram_gb") or mem["total_gb"]
    reserved = p.get("dedicated_gb") or 0
    visible = mem["total_gb"]
    shared = p.get("shared_limit_gb") or 0
    return {"installed_gb": installed, "gpu_reserved_gb": reserved, "windows_gb": visible,
            "hardware_reserved_gb": max(0.0, installed - visible),
            "shared_limit_gb": shared, "shared_fraction": (shared / visible) if visible else 0.5}


def is_unified(info: dict[str, Any] | None = None) -> bool:
    """True when the primary GPU shares system RAM (integrated / unified memory).
    The RAM split planner and planned-split estimates only apply then."""
    info = info or static_info()
    return (info.get("primary") or {}).get("kind") == "integrated"


def plan() -> dict[str, Any] | None:
    """Saved what-if split (hub.json hardware.plan), used for fit estimates when enabled
    and only on unified-memory machines."""
    pl = CONFIG.hub["hardware"].get("plan") or {}
    return pl if pl.get("enabled") and is_unified() else None


def planned_budget(gpu_reserved_gb: float, shared_fraction: float,
                   mem_available_gb: float | None = None) -> dict[str, Any]:
    """Budget for a different BIOS split on this unified-memory machine (a what-if:
    nothing is changed on the PC)."""
    info = static_info()
    hw = CONFIG.hub["hardware"]
    installed = info.get("installed_ram_gb") or 0
    windows = max(0.0, installed - gpu_reserved_gb)
    shared_limit = windows * shared_fraction
    reserve, win_reserve, stack = hw.get("gpu_reserve_gb", 2), hw.get("windows_reserve_gb", 8), turnstone_stack_gb()
    ram_for_models = max(0.0, windows - win_reserve - stack)
    from_reserved = max(0.0, gpu_reserved_gb - reserve)
    from_shared = min(shared_limit, ram_for_models)
    return {"installed_gb": installed, "gpu_reserved_gb": gpu_reserved_gb, "windows_gb": windows,
            "shared_fraction": shared_fraction, "shared_limit_gb": shared_limit,
            "ram_for_models_gb": ram_for_models, "gpu_capacity_gb": from_reserved + from_shared,
            "from_reserved_gb": from_reserved, "from_shared_gb": from_shared,
            "windows_left_with_models_gb": windows - from_shared,
            "reserve_gb": reserve, "windows_reserve_gb": win_reserve, "turnstone_stack_gb": stack}


def budget(mem: dict[str, float] | None = None) -> dict[str, Any]:
    """GPU/RAM capacity for models: static capacity (for 'fit') and free right now."""
    from . import system
    info = static_info()
    hw = CONFIG.hub["hardware"]
    mem = mem or system.memory()
    p = info.get("primary") or {}
    usage = pdh.gpu_memory_usage()
    u = next((v for k, v in usage.items() if k.lower() == (p.get("luid") or "").lower()), {})
    reserve = hw.get("gpu_reserve_gb", 2)
    win_reserve = hw.get("windows_reserve_gb", 8)
    stack = turnstone_stack_gb()
    kind = p.get("kind") or "none"

    dedicated = hw.get("gpu_dedicated_gb") or p.get("dedicated_gb") or 0
    shared_limit = hw.get("gpu_shared_gb") if hw.get("gpu_shared_gb") is not None else p.get("shared_limit_gb") or 0
    used_ded, used_sh = u.get("dedicated_gb"), u.get("shared_gb")
    ram_for_models = max(0.0, mem["total_gb"] - win_reserve - stack)  # RAM not needed by Windows + Turnstone

    if kind == "integrated":
        gpu_capacity = max(0.0, dedicated - reserve) + min(shared_limit, ram_for_models)
        offload_capacity = gpu_capacity  # CPU offload uses the same RAM; no extra room
        free_now = (max(0.0, dedicated - (used_ded or 0) - reserve)
                    + max(0.0, min(shared_limit - (used_sh or 0), mem["available_gb"] - 4)))
    elif kind == "discrete":
        gpu_capacity = max(0.0, dedicated - reserve)
        offload_capacity = gpu_capacity + ram_for_models
        free_now = max(0.0, dedicated - (used_ded or 0) - reserve)
    else:  # no usable GPU: CPU only
        gpu_capacity = 0.0
        offload_capacity = ram_for_models
        free_now = max(0.0, mem["available_gb"] - 4)
    pl = plan() if kind == "integrated" else None
    fit_dedicated = dedicated
    if pl:  # fit estimates follow the planned BIOS split instead of the current one
        pb = planned_budget(pl["gpu_reserved_gb"], pl["shared_fraction"])
        gpu_capacity = offload_capacity = pb["gpu_capacity_gb"]
        fit_dedicated = pl["gpu_reserved_gb"]
    return {
        "plan": pl, "split": split(info, mem) if kind == "integrated" else None,
        "fit_dedicated_gb": fit_dedicated,
        "kind": kind, "name": p.get("name"), "vendor": p.get("vendor"), "luid": p.get("luid"),
        "dedicated_gb": dedicated, "shared_limit_gb": shared_limit,
        "used_dedicated_gb": used_ded, "used_shared_gb": used_sh,
        "reserve_gb": reserve, "windows_reserve_gb": win_reserve, "turnstone_stack_gb": stack,
        "ram_for_models_gb": ram_for_models,
        "gpu_capacity_gb": gpu_capacity, "offload_capacity_gb": offload_capacity, "free_now_gb": free_now,
        "sources": {
            "dedicated": "configured" if hw.get("gpu_dedicated_gb") else ("detected (DXGI)" if p else "none"),
            "shared_limit": "configured" if hw.get("gpu_shared_gb") is not None else ("detected (DXGI)" if p else "none"),
            "usage": "detected (Windows GPU counters)" if u else "unavailable",
            "kind": ("configured (gpu_kind_override)" if hw.get("gpu_kind_override")
                     else "detected (DXGI + RAM carve-out check)" if p else "no GPU found"),
        },
    }


def summary() -> dict[str, Any]:
    info = static_info()
    bw = effective_bandwidth(info)
    hw = CONFIG.hub["hardware"]
    primary = info.get("primary") or {}
    return {
        "unified_memory": is_unified(info),
        "kind": primary.get("kind") or "none",
        "kind_source": "configured (gpu_kind_override)" if hw.get("gpu_kind_override") else "detected",
        "adapters": info["adapters"], "primary": info["primary"], "backend_devices": info["backend_devices"],
        "installed_ram_gb": info["installed_ram_gb"], "ram_bandwidth": info["ram_bandwidth"],
        "effective_bandwidth": bw,
        "effective_tflops": {"value": hw.get("effective_tflops", 8),
                             "source": "configured (hub.json); benchmark calibrates speed"},
        "detected_at": info["detected_at"],
    }
