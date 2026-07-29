"""OpenRouter provider adapter.

- Auth: Bearer token
- Models endpoint: GET /api/v1/models?limit=500 (paginate to get all)
- Returns 400+ models; we return all of them for the user to choose.
"""

from __future__ import annotations
import requests
from . import ProviderAdapter, ModelInfo, AuthError, register

_BASE = "https://openrouter.ai/api/v1"
_DEFAULT = "deepseek/deepseek-chat"


class OpenRouterAdapter(ProviderAdapter):
    @property
    def base_url(self) -> str:
        return _BASE

    @property
    def default_model(self) -> str:
        return _DEFAULT

    def list_models(self, api_key: str) -> list[ModelInfo]:
        headers = {"Authorization": f"Bearer {api_key}"}
        out: list[ModelInfo] = []
        url: str | None = f"{_BASE}/models?limit=500"

        try:
            while url:
                resp = requests.get(url, headers=headers, timeout=20)
                if resp.status_code == 401:
                    raise AuthError("Invalid API key")
                if not resp.ok:
                    raise AuthError(resp.text[:200])

                data = resp.json()
                for m in data.get("data", []):
                    mid = m["id"]
                    name = m.get("name", m.get("id", mid))
                    out.append(ModelInfo(id=mid, name=name))

                # Handle pagination
                links = data.get("links") or {}
                url = links.get("next")

        except AuthError:
            raise
        except Exception as e:
            raise type(e)(f"OpenRouter API unreachable: {e}") from e

        return out


register("openrouter", OpenRouterAdapter())
