"""Memory, speed and Turnstone-usage estimates for a model on this machine.

All formulas and their assumptions are documented in docs/ASSUMPTIONS.md. In short:
  memory  = weights (file size) + KV cache(ctx) + runtime overhead
  decode  ~ effective memory bandwidth / bytes read per token (active weights)
  prefill ~ effective compute / (2 * active parameters)
"""

from __future__ import annotations

import re
from typing import Any

from .config import CONFIG

GB = 1e9           # decimal: file sizes, bandwidth
GIB = 1024 ** 3    # binary: memory capacity, matching Windows / Task Manager "GB"
_ACTIVE_RE = re.compile(r"(?i)[-_]a(\d+(?:\.\d+)?)b(?:[-_.]|$)")


def _text_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
    if not cfg:
        return {}
    for key in ("text_config", "llm_config", "language_config"):
        if isinstance(cfg.get(key), dict):
            return {**cfg, **cfg[key]}
    return cfg


def active_params(total: float | None, name: str, cfg: dict[str, Any] | None) -> tuple[float | None, str]:
    """Parameters used per token. Returns (value, how it was derived)."""
    m = _ACTIVE_RE.search(name or "")
    if m:
        return float(m.group(1)) * 1e9, "name (AxB)"
    c = _text_cfg(cfg)
    experts = c.get("num_local_experts") or c.get("n_routed_experts") or c.get("num_experts")
    k = c.get("num_experts_per_tok") or c.get("experts_per_token") or c.get("moe_topk")
    if total and experts and k and experts > k:
        hidden = c.get("hidden_size") or 0
        inter = c.get("moe_intermediate_size") or c.get("intermediate_size") or 0
        layers = c.get("num_hidden_layers") or 0
        dense = c.get("first_k_dense_replace") or 0
        expert_params = max(layers - dense, 0) * experts * 3 * hidden * inter
        if 0 < expert_params < total:
            return total - expert_params * (1 - k / experts), "config (MoE experts)"
    return total, "dense (all parameters)"


def kv_bytes_per_token(cfg: dict[str, Any] | None) -> tuple[float | None, int | None, str]:
    """KV-cache bytes per token of context, plus sliding-window size if any.

    Returns (bytes_per_token_full_attention, sliding_window, method)."""
    c = _text_cfg(cfg)
    if not c:
        return None, None, "unknown (no config.json)"
    elem = CONFIG.hub["estimates"]["kv_bytes_per_element"]
    layers = c.get("num_hidden_layers") or c.get("n_layer") or 0
    if c.get("kv_lora_rank"):  # Multi-head latent attention (DeepSeek, Kimi K2)
        per = layers * (c["kv_lora_rank"] + (c.get("qk_rope_head_dim") or 64)) * elem
        return per, None, "MLA (compressed KV)"
    heads = c.get("num_attention_heads") or 0
    kv_heads = c.get("num_key_value_heads") or heads
    head_dim = c.get("head_dim") or ((c.get("hidden_size") or 0) // heads if heads else 0)
    if not (layers and kv_heads and head_dim):
        return None, None, "unknown (config incomplete)"
    full_layers, method = layers, "full attention"
    window = c.get("sliding_window") if c.get("use_sliding_window", True) else None
    types = c.get("layer_types")
    pattern = c.get("hybrid_override_pattern")
    if isinstance(types, list) and types:
        full_layers = sum(1 for t in types if "full" in str(t)) or layers
        linear = sum(1 for t in types if "linear" in str(t) or "mamba" in str(t))
        sliding = sum(1 for t in types if "sliding" in str(t))
        method = f"{full_layers} full-attention layers" + (f", {sliding} sliding" if sliding else "") + (f", {linear} linear" if linear else "")
        if not sliding:
            window = None
    elif isinstance(pattern, str) and pattern:
        full_layers = pattern.count("*") or layers
        method = f"hybrid: {full_layers} attention layers of {len(pattern)}"
        window = None
    elif c.get("full_attention_interval"):
        full_layers = max(1, layers // int(c["full_attention_interval"]))
        method = f"hybrid: {full_layers} full-attention layers"
        window = None
    per_layer = 2 * kv_heads * head_dim * elem
    per_token = full_layers * per_layer
    if window and isinstance(types, list):
        sliding_layers = len(types) - full_layers
        return per_token, int(window), method + f" (+{sliding_layers} layers capped at {window} tokens)" if sliding_layers else method
    return per_token, None, method


def estimate(*, name: str, weights_bytes: float, total_params: float | None,
             cfg: dict[str, Any] | None, ctx: int | None = None, parallel: int | None = None,
             gpu: dict[str, Any] | None = None, other_loaded_gb: float = 0,
             prompt_tokens: int | None = None, output_tokens: int | None = None) -> dict[str, Any]:
    hub = CONFIG.hub
    est_cfg, hw = hub["estimates"], hub["hardware"]
    ctx = ctx or est_cfg["default_ctx"]
    parallel = parallel or est_cfg["default_parallel"]
    gpu = gpu or {}

    # Memory
    kv_tok, window, kv_method = kv_bytes_per_token(cfg)
    if kv_tok is None:
        kv_total = 0.10 * weights_bytes * (ctx * parallel / 32768)  # fallback heuristic
        kv_method = "heuristic: 10% of weights per 32k tokens"
    else:
        kv_total = kv_tok * ctx * parallel
        c = _text_cfg(cfg)
        types = c.get("layer_types") or []
        if window and types:
            sliding_layers = sum(1 for t in types if "sliding" in str(t))
            per_layer = kv_tok / max(1, sum(1 for t in types if "full" in str(t)))
            kv_total += sliding_layers * per_layer * min(window, ctx) * parallel
    overhead = est_cfg["runtime_overhead_gb"] * GIB
    need = weights_bytes + kv_total + overhead

    # Fit against the detected memory budget (hardware.budget, values in GiB like Windows)
    kind = gpu.get("kind", "unknown")
    dedicated = gpu.get("fit_dedicated_gb", gpu.get("dedicated_gb")) or 0  # planned split if one is active
    dedicated_cap = max(0.0, dedicated - (gpu.get("reserve_gb") or 0)) * GIB
    gpu_cap = (gpu.get("gpu_capacity_gb") or 0) * GIB
    offload_cap = (gpu.get("offload_capacity_gb") or 0) * GIB
    if not gpu:
        fit = "unknown"
    elif kind in ("integrated", "discrete") and need <= dedicated_cap:
        fit = "fits in dedicated GPU memory"
    elif kind == "integrated" and need <= gpu_cap:
        fit = "fits using shared memory"
    elif kind == "discrete" and need <= offload_cap:
        fit = "partial CPU offload (slow)"
    elif kind == "none" and need <= offload_cap:
        fit = "CPU only (slow)"
    else:
        fit = "does not fit"
    fits_now = bool(gpu) and fit != "does not fit" and need <= (gpu.get("free_now_gb") or 0) * GIB + other_loaded_gb * GIB

    # Speed
    from . import hardware
    info = hardware.static_info()
    bw = hardware.effective_bandwidth(info)["gbps"] * GB
    active, active_method = active_params(total_params, name, cfg)
    bpw = (weights_bytes * 8 / total_params) if total_params else None
    tg = pp = None
    if active and bpw:
        bytes_per_token = active * bpw / 8
        cpu_share = 0.0
        if fit in ("partial CPU offload (slow)", "CPU only (slow)"):
            cpu_share = 1.0 if kind == "none" else max(0.0, min(1.0, (need - gpu_cap) / max(weights_bytes, 1)))
        gpu_bw = bw if kind != "none" else hardware.cpu_bandwidth(info) * GB
        cpu_bw = hardware.cpu_bandwidth(info) * GB
        seconds = bytes_per_token * ((1 - cpu_share) / gpu_bw + cpu_share / cpu_bw)
        tg = 1 / seconds
        pp = hw["effective_tflops"] * 1e12 / (2 * active) * (1 if cpu_share == 0 else 0.25)

    # Turnstone agent turn
    p_tok = prompt_tokens or est_cfg["agent_turn_prompt_tokens"]
    o_tok = output_tokens or est_cfg["agent_turn_output_tokens"]
    turn_s = (p_tok / pp + o_tok / tg) if (tg and pp) else None
    return {
        "ctx_total": ctx * parallel, "ctx_per_request": ctx, "parallel": parallel,
        "weights_gb": weights_bytes / GIB, "kv_cache_gb": kv_total / GIB, "overhead_gb": overhead / GIB,
        "memory_needed_gb": need / GIB, "kv_method": kv_method,
        "fit": fit, "fits_now": fits_now, "gpu_kind": kind,
        "total_params": total_params, "active_params": active, "active_method": active_method,
        "bits_per_weight": bpw,
        "decode_tokens_per_s": tg, "prefill_tokens_per_s": pp,
        "agent_turn": {"prompt_tokens": p_tok, "output_tokens": o_tok, "seconds": turn_s,
                       "turns_per_hour": (3600 / turn_s * parallel) if turn_s else None},
    }


def cloud_estimate(entry: dict[str, Any], prompt_tokens: int | None = None,
                   output_tokens: int | None = None) -> dict[str, Any]:
    est_cfg = CONFIG.hub["estimates"]
    p_tok = prompt_tokens or est_cfg["agent_turn_prompt_tokens"]
    o_tok = output_tokens or est_cfg["agent_turn_output_tokens"]
    ip, op = entry.get("input_per_mtok"), entry.get("output_per_mtok")
    cost = (p_tok * ip + o_tok * op) / 1e6 if (ip is not None and op is not None) else None
    tps = entry.get("tokens_per_second")
    return {**entry, "agent_turn": {"prompt_tokens": p_tok, "output_tokens": o_tok,
                                    "cost": cost, "seconds": (o_tok / tps) if tps else None,
                                    "cost_per_1000_turns": cost * 1000 if cost is not None else None}}


def local_energy_cost(turn_seconds: float | None) -> float | None:
    pr = CONFIG.pricing
    if not turn_seconds or pr.get("electricity_per_kwh") is None:
        return None
    return pr["local_power_watts"] / 1000 * turn_seconds / 3600 * pr["electricity_per_kwh"]
