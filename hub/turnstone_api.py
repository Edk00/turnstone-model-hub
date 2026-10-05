"""Turnstone console admin API client (model definitions).

Needs an API token with read,write,approve scopes, created by you:
  wsl -d Ubuntu-24.04 --cd ~/turnstone -- docker compose exec node-1 \
      turnstone-admin create-token --user <admin user id> --scopes read,write,approve --name model-hub
Save the token in model-hub/config/turnstone.token (git-ignored) or set
MODEL_HUB_TURNSTONE_TOKEN. Without a token the Turnstone panel is read-only.
"""

from __future__ import annotations

from typing import Any

from . import net
from .config import CONFIG


class TurnstoneError(Exception):
    pass


def _base() -> str:
    return CONFIG.hub["turnstone"]["console_url"].rstrip("/") + "/v1/api/admin"


def _headers() -> dict[str, str]:
    token = CONFIG.turnstone_token()
    if not token:
        raise TurnstoneError("no Turnstone API token configured (see docs/INTEGRATION.md)")
    return {"Authorization": f"Bearer {token}"}


def has_token() -> bool:
    return bool(CONFIG.turnstone_token())


def model_definitions() -> dict[str, Any]:
    """{"models": [...], "default_alias": str} as returned by the console."""
    try:
        data = net.get_json(f"{_base()}/model-definitions", headers=_headers(), timeout=5)
    except net.FetchError as e:
        raise TurnstoneError(str(e)) from e
    return {"models": [m for m in data.get("models", []) if isinstance(m, dict)],
            "default_alias": data.get("default_alias")}


def update(definition_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    try:
        return net.get_json(f"{_base()}/model-definitions/{definition_id}", method="PUT", body=fields,
                            headers=_headers(), timeout=10)
    except net.FetchError as e:
        raise TurnstoneError(str(e)) from e


def reload() -> None:
    try:
        net.get_json(f"{_base()}/model-definitions/reload", method="POST", body={}, headers=_headers(), timeout=20)
    except net.FetchError as e:
        raise TurnstoneError(str(e)) from e


def register(alias: str, model: str, base_url: str, context_window: int,
             provider: str = "openai-compatible") -> dict[str, Any]:
    body = {"alias": alias, "provider": provider, "model": model, "base_url": base_url,
            "context_window": context_window, "enabled": True}
    try:
        created = net.get_json(f"{_base()}/model-definitions", method="POST", body=body,
                               headers=_headers(), timeout=10)
        net.get_json(f"{_base()}/model-definitions/reload", method="POST", body={},
                     headers=_headers(), timeout=20)
    except net.FetchError as e:
        raise TurnstoneError(str(e)) from e
    return created
