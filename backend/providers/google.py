"""Google (Gemini) provider adapter.

- Auth: API key as ?key= query param (Google REST API standard).
- Models endpoint: GET /v1beta/models?key=...
- Chat endpoint: OpenAI-compatible via /v1beta/...
- Filter: only models supporting 'generateContent'.
"""

from __future__ import annotations
import requests
from . import ProviderAdapter, ModelInfo, AuthError, register

_BASE = "https://generativelanguage.googleapis.com/v1beta/"
_DEFAULT = "gemini-2.5-flash"


class GoogleAdapter(ProviderAdapter):
    @property
    def base_url(self) -> str:
        return _BASE

    @property
    def default_model(self) -> str:
        return _DEFAULT

    def list_models(self, api_key: str) -> list[ModelInfo]:
        url = f"{_BASE}models?key={api_key}"
        try:
            resp = requests.get(url, timeout=15)
        except Exception as e:
            raise type(e)(f"Google API unreachable: {e}") from e

        if not resp.ok:
            # Extract the actual error from JSON body
            try:
                err_body = resp.json()
                msg = (err_body.get("error") or {}).get("message", "")
            except Exception:
                msg = ""
            if not msg:
                msg = resp.text[:200].strip()
            if not msg:
                msg = f"Google API error (HTTP {resp.status_code})"
            raise AuthError(msg)

        data = resp.json()
        out: list[ModelInfo] = []
        for m in data.get("models", []):
            methods = m.get("supportedGenerationMethods", [])
            if "generateContent" in methods:
                raw = m["name"]  # "models/gemini-2.5-flash"
                mid = raw.replace("models/", "", 1) if raw.startswith("models/") else raw
                display = mid.replace("-", " ").title().replace("2 5", "2.5").replace("3 1", "3.1")
                out.append(ModelInfo(id=mid, name=display))
        return out


register("google", GoogleAdapter())
