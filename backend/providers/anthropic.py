"""Anthropic (Claude) provider adapter.

- Auth: x-api-key header for the Models API; Bearer for OpenAI-compatible chat.
- Models endpoint: GET /v1/models  (requires x-api-key + anthropic-version)
- Chat endpoint: OpenAI-compatible via /v1/ (uses Bearer token via OpenAI SDK)
"""

from __future__ import annotations
import requests
from . import ProviderAdapter, ModelInfo, AuthError, register

_BASE = "https://api.anthropic.com/v1/"
_DEFAULT = "claude-sonnet-5-20250202"


class AnthropicAdapter(ProviderAdapter):
    @property
    def base_url(self) -> str:
        return _BASE

    @property
    def default_model(self) -> str:
        return _DEFAULT

    def list_models(self, api_key: str) -> list[ModelInfo]:
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        try:
            resp = requests.get(f"{_BASE}/models?limit=200", headers=headers, timeout=15)
        except Exception as e:
            raise type(e)(f"Anthropic API unreachable: {e}") from e

        if resp.status_code == 401:
            raise AuthError("Invalid API key")
        if not resp.ok:
            # Normalize Anthropic's verbose error response
            try:
                err_body = resp.json()
                msg = ((err_body.get("error") or {}).get("message", "") or
                       (err_body.get("error") or {}).get("type", "") or
                       resp.text[:100])
            except Exception:
                msg = resp.text[:100]
            raise AuthError(msg)

        data = resp.json()
        out: list[ModelInfo] = []
        for m in data.get("data", []):
            mid = m["id"]
            name = m.get("display_name", mid)
            out.append(ModelInfo(id=mid, name=name))
        return out


register("anthropic", AnthropicAdapter())
