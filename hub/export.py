"""Dashboard export: JSON snapshot, CSV model table and a self-contained HTML report."""

from __future__ import annotations

import csv
import datetime as dt
import html
import io
import json
from typing import Any

from . import __version__, estimates, local_models, system
from .config import CONFIG


def snapshot(include_discovery: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "generator": f"turnstone-model-hub {__version__}",
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "system": system.snapshot(),
        "local_models": local_models.list_all(),
        "cloud": [estimates.cloud_estimate(c) for c in CONFIG.pricing.get("cloud", [])],
        "assumptions": {"hardware": CONFIG.hub["hardware"], "estimates": CONFIG.hub["estimates"]},
        **(include_discovery or {}),
    }


def _fmt(v: Any, nd: int = 1) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


CSV_COLUMNS = ["name", "source", "repo", "file", "size_gb", "params_b", "active_params_b", "used_for", "tools",
               "memory_needed_gb", "fit", "decode_tok_s_est", "decode_tok_s_measured", "agent_turn_s", "in_turnstone"]


def model_rows(snap: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for m in snap["local_models"]["models"]:
        e = m["estimate"]
        rows.append({
            "name": m["name"], "source": "local", "repo": m.get("repo"), "file": m.get("file"),
            "size_gb": _fmt(m.get("size_gb")), "params_b": _fmt((m.get("params") or 0) / 1e9),
            "active_params_b": _fmt((e.get("active_params") or 0) / 1e9),
            "used_for": "; ".join(m.get("used_for") or []), "tools": m.get("supports_tools"),
            "memory_needed_gb": _fmt(e.get("memory_needed_gb")), "fit": e.get("fit"),
            "decode_tok_s_est": _fmt(e.get("decode_tokens_per_s"), 0),
            "decode_tok_s_measured": _fmt((m.get("benchmark") or {}).get("decode_tokens_per_s"), 0),
            "agent_turn_s": _fmt(e["agent_turn"].get("seconds"), 0), "in_turnstone": m.get("in_turnstone"),
        })
    for c in snap.get("watchlist") or []:
        b = c.get("best_fit") or {}
        e = b.get("estimate") or {}
        rows.append({
            "name": c["id"], "source": "watchlist", "repo": (c.get("gguf") or {}).get("id"), "file": b.get("variant"),
            "size_gb": _fmt(b.get("size_gb")), "params_b": _fmt((c.get("params") or 0) / 1e9),
            "active_params_b": _fmt((e.get("active_params") or 0) / 1e9), "used_for": c.get("pipeline_tag"),
            "tools": "", "memory_needed_gb": _fmt(e.get("memory_needed_gb")),
            "fit": e.get("fit") or "does not fit", "decode_tok_s_est": _fmt(e.get("decode_tokens_per_s"), 0),
            "decode_tok_s_measured": "", "agent_turn_s": _fmt((e.get("agent_turn") or {}).get("seconds"), 0),
            "in_turnstone": "",
        })
    return rows


def to_csv(snap: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    w.writeheader()
    w.writerows(model_rows(snap))
    return buf.getvalue()


def to_html(snap: dict[str, Any]) -> str:
    s = snap["system"]
    gpu, mem, disk = s["gpu"], s["memory"], s["disk"]
    e = html.escape
    res = [("CPU", f"{e(s['cpu']['name'])} - {s['cpu']['cores']} threads, {s['cpu']['percent']}% busy"),
           ("RAM (Windows)", f"{mem['used_gb']:.1f} used / {mem['total_gb']:.1f} GB ({mem['available_gb']:.1f} free); {mem.get('installed_gb') or '?'} GB installed"),
           ("GPU", f"{e(str(gpu.get('name')))}: {gpu.get('dedicated_gb') or 0:.0f} GB dedicated, "
                   f"{gpu.get('usable_total_gb') or 0:.1f} GB usable, {gpu.get('free_gb') or 0:.1f} GB free"),
           ("Disk (models)", f"{disk['used_gb']:.0f} used / {disk['total_gb']:.0f} GB ({disk['available_gb']:.0f} free); models {disk['models_gb']:.1f} GB"),
           ("Turnstone", "up, %s node(s)" % s["turnstone"].get("nodes") if s["turnstone"].get("up") else "not running")]
    rows = model_rows(snap)
    table = "".join("<tr>" + "".join(f"<td>{e(str(r[c]))}</td>" for c in CSV_COLUMNS) + "</tr>" for r in rows)
    head = "".join(f"<th>{c}</th>" for c in CSV_COLUMNS)
    resources = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in res)
    assumptions = e(json.dumps(snap["assumptions"], indent=2))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Model Hub report</title>
<style>body{{font:14px system-ui,sans-serif;margin:24px;color:#1b1f24}}table{{border-collapse:collapse;width:100%;margin:12px 0}}
th,td{{border:1px solid #d0d7de;padding:4px 8px;text-align:left;vertical-align:top}}th{{background:#f3f5f7}}
pre{{background:#f6f8fa;padding:12px;overflow:auto}}h1{{margin:0 0 4px}}small{{color:#57606a}}</style></head><body>
<h1>Model Hub report</h1><small>{e(snap['generator'])} - {e(snap['generated'])} - host {e(s['host'])}</small>
<h2>System resources</h2><table>{resources}</table>
<h2>Models</h2><div style="overflow:auto"><table><tr>{head}</tr>{table}</table></div>
<h2>Assumptions</h2><pre>{assumptions}</pre>
<p><small>Estimates are approximations; see docs/ASSUMPTIONS.md in the Model Hub source.</small></p>
</body></html>"""
