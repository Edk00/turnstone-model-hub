"""Minimal GGUF metadata reader (header key/values only; tensors are not read).

Used to get architecture details (layers, KV heads, experts) for memory and speed
estimates when a model's config.json is unavailable (e.g. gated repos), and to
check that a downloaded file really is GGUF. Spec: github.com/ggml-org/ggml/blob/master/docs/gguf.md
"""

from __future__ import annotations

import io
import struct
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO

from .net import USER_AGENT

MAGIC = b"GGUF"
_SCALARS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
_STRING, _ARRAY = 8, 9
_MAX_ARRAY_KEEP = 4096  # keep small arrays (per-layer values); skip tokenizer vocabularies


class GGUFError(Exception):
    pass


def _read(f: BinaryIO, n: int) -> bytes:
    b = f.read(n)
    if len(b) != n:
        raise GGUFError("truncated GGUF header")
    return b


def _string(f: BinaryIO) -> str:
    (n,) = struct.unpack("<Q", _read(f, 8))
    return _read(f, n).decode("utf-8", "replace")


def _value(f: BinaryIO, t: int) -> Any:
    if t in _SCALARS:
        fmt = _SCALARS[t]
        return struct.unpack(fmt, _read(f, struct.calcsize(fmt)))[0]
    if t == _STRING:
        return _string(f)
    if t == _ARRAY:
        (et,) = struct.unpack("<I", _read(f, 4))
        (n,) = struct.unpack("<Q", _read(f, 8))
        if et in _SCALARS and n > _MAX_ARRAY_KEEP:
            f.seek(n * struct.calcsize(_SCALARS[et]), io.SEEK_CUR)
            return None
        items = [_value(f, et) for _ in range(n)]
        return items if n <= _MAX_ARRAY_KEEP else None
    raise GGUFError(f"unknown GGUF value type {t}")


def read_metadata(f: BinaryIO) -> dict[str, Any]:
    if _read(f, 4) != MAGIC:
        raise GGUFError("not a GGUF file (bad magic)")
    (version,) = struct.unpack("<I", _read(f, 4))
    tensors, kv_count = struct.unpack("<QQ", _read(f, 16))
    meta: dict[str, Any] = {"gguf.version": version, "gguf.tensor_count": tensors}
    for _ in range(kv_count):
        key = _string(f)
        (t,) = struct.unpack("<I", _read(f, 4))
        meta[key] = _value(f, t)
    return meta


def read_local(path: Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        return read_metadata(f)


def read_remote(url: str, max_bytes: int = 24 * 1024 * 1024) -> dict[str, Any]:
    """Read metadata from the start of a remote GGUF using an HTTP range request."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Range": f"bytes=0-{max_bytes - 1}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read(max_bytes)
    return read_metadata(io.BytesIO(data))


def is_gguf(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == MAGIC
    except OSError:
        return False


def to_config(meta: dict[str, Any]) -> dict[str, Any]:
    """Translate GGUF metadata into the config.json-style keys estimates.py reads."""
    arch = meta.get("general.architecture", "")
    g = lambda k: meta.get(f"{arch}.{k}")  # noqa: E731
    layers = g("block_count")
    heads = g("attention.head_count")
    kv = g("attention.head_count_kv")
    cfg: dict[str, Any] = {
        "model_type": arch, "num_hidden_layers": layers,
        "hidden_size": g("embedding_length"),
        "max_position_embeddings": g("context_length"),
        "head_dim": g("attention.key_length"),
        "num_local_experts": g("expert_count"),
        "num_experts_per_tok": g("expert_used_count"),
        "moe_intermediate_size": g("expert_feed_forward_length"),
        "intermediate_size": g("feed_forward_length") if isinstance(g("feed_forward_length"), int) else None,
    }
    if isinstance(heads, list):
        heads = max(heads) if heads else None
    cfg["num_attention_heads"] = heads
    n = layers or 0
    if isinstance(kv, list):  # per-layer; 0 = no attention in that layer (hybrid models)
        attn_layers = [k for k in kv if k]
        cfg["num_key_value_heads"] = max(attn_layers) if attn_layers else None
        types = ["full_attention" if k else "linear_attention" for k in kv]
    else:
        cfg["num_key_value_heads"] = kv
        types = ["full_attention"] * n

    # Sliding-window attention: those layers only cache `window` tokens.
    window = g("attention.sliding_window")
    pattern = g("attention.sliding_window_pattern")
    if window and n:
        if isinstance(pattern, list) and len(pattern) == n:
            sliding = [bool(p) for p in pattern]
        elif arch == "gemma3":  # 5 local : 1 global (llama.cpp default for Gemma 3)
            sliding = [(i + 1) % 6 != 0 for i in range(n)]
        elif arch in ("gemma2", "gpt-oss"):  # alternating local/global
            sliding = [i % 2 == 0 for i in range(n)]
        else:
            sliding = [False] * n
        types = ["sliding_attention" if s and t == "full_attention" else t for s, t in zip(sliding, types)]
        cfg["sliding_window"] = window
    if any(t != "full_attention" for t in types):
        cfg["layer_types"] = types
    if g("attention.kv_lora_rank"):
        cfg["kv_lora_rank"] = g("attention.kv_lora_rank")
        cfg["qk_rope_head_dim"] = g("rope.dimension_count")
    if not cfg["head_dim"] and heads and cfg["hidden_size"]:
        cfg["head_dim"] = cfg["hidden_size"] // heads
    cfg["general.name"] = meta.get("general.name")
    cfg["general.size_label"] = meta.get("general.size_label")
    cfg["chat_template_has_tools"] = "tools" in (meta.get("tokenizer.chat_template") or "")
    return {k: v for k, v in cfg.items() if v is not None}
