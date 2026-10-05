"""Configuration loading and the shared local-model registry (models.json)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent  # model-hub/
CONFIG_DIR = ROOT / "config"

_lock = threading.RLock()


def _read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def resolve(p: str) -> Path:
    """Resolve a path from hub.json relative to the model-hub folder."""
    path = Path(p)
    return path if path.is_absolute() else (ROOT / path).resolve()


class Config:
    def __init__(self) -> None:
        self.hub: dict[str, Any] = _read_json(CONFIG_DIR / "hub.json")
        self.sources_doc: dict[str, Any] = _read_json(CONFIG_DIR / "sources.json")
        self.watchlist: dict[str, Any] = _read_json(CONFIG_DIR / "watchlist.json")
        self.pricing: dict[str, Any] = _read_json(CONFIG_DIR / "pricing.json")

    # -- paths ---------------------------------------------------------------
    @property
    def models_dir(self) -> Path:
        return resolve(self.hub["models_dir"])

    @property
    def registry_path(self) -> Path:
        return resolve(self.hub["models_registry"])

    @property
    def llama_server(self) -> Path:
        return resolve(self.hub["llama_server"])

    @property
    def logs_dir(self) -> Path:
        return resolve(self.hub["logs_dir"])

    @property
    def token_path(self) -> Path:
        return CONFIG_DIR / self.hub["turnstone"]["token_file"]

    # -- local model registry (shared with llm-start.ps1) --------------------
    def registry(self) -> list[dict[str, Any]]:
        with _lock:
            return _read_json(self.registry_path)["models"]

    def save_registry_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Add or replace a model in models.json, assigning a free port if needed."""
        with _lock:
            doc = _read_json(self.registry_path)
            models = doc["models"]
            existing = {m["name"]: m for m in models}
            if "port" not in entry or not entry["port"]:
                used = {m["port"] for m in models}
                port = 8001
                while port in used:
                    port += 1
                entry["port"] = port
            entry.setdefault("ctx", self.hub["estimates"]["default_ctx"] * 2)
            entry.setdefault("parallel", self.hub["estimates"]["default_parallel"])
            if entry["name"] in existing:
                models[models.index(existing[entry["name"]])] = {**existing[entry["name"]], **entry}
            else:
                models.append(entry)
            _write_json(self.registry_path, doc)
            return entry

    # -- sources -------------------------------------------------------------
    def sources(self) -> list[dict[str, Any]]:
        return self.sources_doc["sources"]

    def save_sources(self) -> None:
        with _lock:
            _write_json(CONFIG_DIR / "sources.json", self.sources_doc)

    def save_hub(self) -> None:
        with _lock:
            _write_json(CONFIG_DIR / "hub.json", self.hub)

    def turnstone_token(self) -> str | None:
        env = os.environ.get("MODEL_HUB_TURNSTONE_TOKEN")
        if env:
            return env.strip()
        try:
            return self.token_path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None


CONFIG = Config()
