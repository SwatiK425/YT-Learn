"""OpenAI provider adapter.

- Auth: Bearer token (standard OpenAI SDK)
- Models endpoint: GET /v1/models
- Filter: chat-capable models by naming convention (gpt-, o1-, o3- prefixes)
"""

from __future__ import annotations
import requests
from . import ProviderAdapter, ModelInfo, AuthError, register

_BASE = "https://api.openai.com/v1"
_DEFAULT = "gpt-4o-mini"

# Prefixes that indicate chat-completion-capable models
_CHAT_PREFIXES = ("gpt-", "o1-", "o3-", "o4-")  # o4- for future


class OpenAIAdapter(ProviderAdapter):
    @property
    def base_url(self) -> str:
        return _BASE

    @property
    def default_model(self) -> str:
        return _DEFAULT

    def list_models(self, api_key: str) -> list[ModelInfo]:
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            resp = requests.get(f"{_BASE}/models", headers=headers, timeout=15)
        except Exception as e:
            raise type(e)(f"OpenAI API unreachable: {e}") from e

        if resp.status_code == 401:
            raise AuthError("Invalid API key")
        if not resp.ok:
            raise AuthError(resp.text[:200])

        data = resp.json()
        out: list[ModelInfo] = []
        for m in data.get("data", []):
            mid = m["id"]
            # Only include chat-capable models
            if mid.startswith(_CHAT_PREFIXES):
                out.append(ModelInfo(id=mid, name=mid))
        return out


register("openai", OpenAIAdapter())
