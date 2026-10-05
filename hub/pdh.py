"""System-wide GPU memory usage per adapter from Windows performance counters
("GPU Adapter Memory", the same source Task Manager uses), via pdh.dll and ctypes."""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from typing import Any

_PDH_FMT_DOUBLE = 0x00000200
_PDH_MORE_DATA = 0x800007D2


class _VALUE(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


class _ITEM(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", _VALUE)]


class _Query:
    def __init__(self, paths: dict[str, str]) -> None:
        self.pdh = ctypes.WinDLL("pdh")
        self.q = ctypes.c_void_p()
        if self.pdh.PdhOpenQueryW(None, None, ctypes.byref(self.q)) != 0:
            raise OSError("PdhOpenQuery failed")
        self.counters = {}
        for key, path in paths.items():
            h = ctypes.c_void_p()
            if self.pdh.PdhAddEnglishCounterW(self.q, path, None, ctypes.byref(h)) == 0:
                self.counters[key] = h
        self.pdh.PdhCollectQueryData(self.q)

    def read(self) -> dict[str, dict[str, float]]:
        self.pdh.PdhCollectQueryData(self.q)
        out: dict[str, dict[str, float]] = {}
        for key, h in self.counters.items():
            size, count = wintypes.DWORD(0), wintypes.DWORD(0)
            rc = self.pdh.PdhGetFormattedCounterArrayW(h, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None)
            if (rc & 0xFFFFFFFF) != _PDH_MORE_DATA or not size.value:
                continue
            buf = (ctypes.c_byte * size.value)()
            if self.pdh.PdhGetFormattedCounterArrayW(h, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), buf) != 0:
                continue
            items = ctypes.cast(buf, ctypes.POINTER(_ITEM))
            vals: dict[str, float] = {}
            for i in range(count.value):
                name = items[i].szName or ""
                luid = "_".join(name.split("_")[:3])  # luid_0xHHHHHHHH_0xLLLLLLLL
                vals[luid] = vals.get(luid, 0.0) + items[i].FmtValue.doubleValue
            out[key] = vals
        return out


_query: _Query | None = None
_last: tuple[float, dict[str, dict[str, float]]] = (0.0, {})


def gpu_memory_usage(max_age: float = 2.0) -> dict[str, dict[str, float]]:
    """{luid: {"dedicated_gb": x, "shared_gb": y}} system-wide; {} if unavailable."""
    global _query, _last
    if os.name != "nt":
        return {}
    if time.time() - _last[0] < max_age:
        return _last[1]
    try:
        if _query is None:
            _query = _Query({"dedicated": r"\GPU Adapter Memory(*)\Dedicated Usage",
                             "shared": r"\GPU Adapter Memory(*)\Shared Usage"})
        raw = _query.read()
    except OSError:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for kind, vals in raw.items():
        for luid, v in vals.items():
            result.setdefault(luid, {})[f"{kind}_gb"] = v / 1024 ** 3
    _last = (time.time(), result)
    return result
