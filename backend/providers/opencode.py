"""OpenCode Zen provider adapter.

- Auth: Bearer token (same as OpenAI)
- Models endpoint: GET /zen/v1/models (if available)
- Fallback: hardcoded list when the API is unreachable
"""

from __future__ import annotations
import requests
from . import ProviderAdapter, ModelInfo, AuthError, register

_BASE = "https://opencode.ai/zen/v1"
_DEFAULT = "deepseek-v4-flash-free"

# Static fallback if the models API is down or doesn't exist
_FALLBACK_MODELS = [
    ModelInfo("deepseek-v4-flash-free", "DeepSeek V4 Flash (Free)"),
    ModelInfo("hy3-free", "Hy3 (Free)"),
    ModelInfo("north-mini-code-free", "North Mini Code (Free)"),
    ModelInfo("nemotron-3-ultra-free", "Nemotron 3 Ultra (Free)"),
]


class OpenCodeAdapter(ProviderAdapter):
    @property
    def base_url(self) -> str:
        return _BASE

    @property
    def default_model(self) -> str:
        return _DEFAULT

    def list_models(self, api_key: str) -> list[ModelInfo]:
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            resp = requests.get(f"{_BASE}/models", headers=headers, timeout=10)
            if resp.status_code == 401:
                raise AuthError("Invalid API key")
            if resp.ok:
                data = resp.json()
                out: list[ModelInfo] = []
                for m in data.get("data", []):
                    mid = m["id"]
                    name = m.get("name", m.get("id", mid))
                    out.append(ModelInfo(id=mid, name=name))
                if out:
                    return out
        except requests.exceptions.ConnectionError:
            pass  # Fall through to static list
        except requests.exceptions.Timeout:
            pass
        except Exception:
            pass

        # API unavailable — return static list so the user can still configure
        return list(_FALLBACK_MODELS)


register("opencode-zen", OpenCodeAdapter())
