"""Windows GPU adapter enumeration via DXGI (vendor-neutral: AMD, NVIDIA, Intel).

DXGI reports, per adapter, the dedicated video memory, the shared system memory
limit (how much RAM Windows lets the GPU borrow) and the LUID that identifies the
adapter in the "GPU Adapter Memory" performance counters Task Manager uses.
Called through raw COM vtables with ctypes (no extra packages).
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Any


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


def _guid(s: str) -> _GUID:
    import uuid
    u = uuid.UUID(s)
    g = _GUID(u.fields[0], u.fields[1], u.fields[2])
    g.Data4[:] = list(u.bytes[8:])
    return g


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _DESC1(ctypes.Structure):
    _fields_ = [("Description", wintypes.WCHAR * 128), ("VendorId", wintypes.UINT), ("DeviceId", wintypes.UINT),
                ("SubSysId", wintypes.UINT), ("Revision", wintypes.UINT),
                ("DedicatedVideoMemory", ctypes.c_size_t), ("DedicatedSystemMemory", ctypes.c_size_t),
                ("SharedSystemMemory", ctypes.c_size_t), ("AdapterLuid", _LUID), ("Flags", wintypes.UINT)]


_IID_FACTORY1 = "770aae78-f26f-4dba-a829-253c83d1b387"
_SOFTWARE = 0x2  # DXGI_ADAPTER_FLAG_SOFTWARE
_VENDORS = {0x1002: "AMD", 0x1022: "AMD", 0x10DE: "NVIDIA", 0x8086: "Intel", 0x1414: "Microsoft", 0x5143: "Qualcomm"}


def _method(obj: ctypes.c_void_p, index: int, restype, *argtypes):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])


def adapters() -> list[dict[str, Any]]:
    """Hardware adapters (software/basic-render adapters excluded)."""
    if os.name != "nt":
        return []
    try:
        dxgi = ctypes.WinDLL("dxgi")
    except OSError:
        return []
    factory = ctypes.c_void_p()
    iid = _guid(_IID_FACTORY1)
    if dxgi.CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory)) != 0:
        return []
    out = []
    try:
        enum1 = _method(factory, 12, ctypes.HRESULT, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))
        i = 0
        while True:
            adapter = ctypes.c_void_p()
            try:
                enum1(factory, i, ctypes.byref(adapter))
            except OSError:  # DXGI_ERROR_NOT_FOUND ends the list
                break
            desc = _DESC1()
            _method(adapter, 10, ctypes.HRESULT, ctypes.POINTER(_DESC1))(adapter, ctypes.byref(desc))
            _method(adapter, 2, wintypes.ULONG)(adapter)  # Release
            i += 1
            if desc.Flags & _SOFTWARE:
                continue
            luid = f"luid_0x{desc.AdapterLuid.HighPart & 0xFFFFFFFF:08x}_0x{desc.AdapterLuid.LowPart:08x}"
            out.append({
                "name": desc.Description, "vendor": _VENDORS.get(desc.VendorId, hex(desc.VendorId)),
                "dedicated_gb": desc.DedicatedVideoMemory / 1024 ** 3,
                "dedicated_system_gb": desc.DedicatedSystemMemory / 1024 ** 3,
                "shared_limit_gb": desc.SharedSystemMemory / 1024 ** 3,
                "luid": luid,
            })
    finally:
        _method(factory, 2, wintypes.ULONG)(factory)
    return out


def classify(adapter: dict[str, Any], installed_gb: float | None, visible_gb: float) -> str:
    """'integrated' (memory carved from system RAM / unified) or 'discrete'."""
    if adapter["vendor"] == "Intel" and adapter["dedicated_gb"] < 2:
        return "integrated"
    # A UMA iGPU's "dedicated" memory is RAM reserved in firmware: installed RAM minus
    # the RAM Windows can see roughly equals it (e.g. 128 GB installed - 64 GB visible = 64 GB reserved).
    if installed_gb and abs((installed_gb - visible_gb) - adapter["dedicated_gb"]) <= max(1.0, 0.1 * adapter["dedicated_gb"]):
        return "integrated"
    if adapter["vendor"] == "AMD" and any(k in adapter["name"] for k in ("Radeon(TM)", "Radeon Graphics", "Vega")) \
            and not any(k in adapter["name"] for k in ("RX ", "PRO W", "Instinct")):
        return "integrated"
    return "discrete"
