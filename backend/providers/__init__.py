"""Provider adapter interface and registry.

Each provider adapter implements three methods:
  - list_models(api_key)  → list[ModelInfo]
  - validate_key(api_key) → bool
  - make_client(api_key)  → OpenAI

Plus two properties:
  - base_url         → str (for the OpenAI SDK)
  - default_model    → str

A `test_connection(provider, api_key)` helper combines validation + model listing
into one call for the /test-connection endpoint.
"""

from __future__ import annotations
import requests
from abc import ABC, abstractmethod
from dataclasses import dataclass
from openai import OpenAI


class AuthError(Exception):
    """Raised when the API key is rejected by the provider."""


class NetworkError(Exception):
    """Raised when the provider API is unreachable."""


@dataclass
class ModelInfo:
    id: str
    name: str


class ProviderAdapter(ABC):
    """Base class for all provider adapters."""

    @property
    @abstractmethod
    def base_url(self) -> str:
        """OpenAI-compatible base URL for chat completions."""
        ...

    @property
    @abstractmethod
    def default_model(self) -> str:
        """Fallback model when the user doesn't pick one."""
        ...

    @abstractmethod
    def list_models(self, api_key: str) -> list[ModelInfo]:
        """Fetch available models from the provider's List Models API.

        Raises AuthError on bad key, NetworkError on unreachable.
        """
        ...

    def validate_key(self, api_key: str) -> bool:
        """Check if an API key is valid by trying to list models.

        Override if a cheaper validate endpoint exists.
        """
        try:
            self.list_models(api_key)
            return True
        except AuthError:
            return False
        except Exception:
            return False

    def make_client(self, api_key: str) -> OpenAI:
        """Create an OpenAI-compatible client for chat completions."""
        return OpenAI(api_key=api_key, base_url=self.base_url)


# ─── Registry ───────────────────────────────────────────────

_registry: dict[str, ProviderAdapter] = {}


def register(provider: str, adapter: ProviderAdapter) -> None:
    _registry[provider] = adapter


def get_adapter(provider: str) -> ProviderAdapter | None:
    return _registry.get(provider)


def known_providers() -> list[str]:
    return sorted(_registry.keys())


# Providers register themselves at module import time.
# Import all adapters here to trigger registration.

from . import google        # noqa: F811, E402
from . import openai_prov   # noqa: F811, E402  (avoid shadowing openai module)
from . import anthropic     # noqa: F811, E402
from . import openrouter    # noqa: F811, E402
from . import opencode      # noqa: F811, E402


# ─── Helper for the test-connection endpoint ────────────────

def test_connection(
    provider: str, api_key: str
) -> dict:
    """Validate key + list models in one call.

    Returns:
      On success: {"valid": True, "default_model": str, "models": [{"id":...,"name":...}]}
      On failure: {"valid": False, "error": str}
    """
    adapter = get_adapter(provider)
    if not adapter:
        return {"valid": False, "error": f"Unknown provider '{provider}'"}

    try:
        models = adapter.list_models(api_key)
        return {
            "valid": True,
            "default_model": adapter.default_model,
            "models": [{"id": m.id, "name": m.name} for m in models],
        }
    except AuthError as e:
        return {"valid": False, "error": str(e) or "Invalid API key"}
    except requests.exceptions.ConnectionError:
        return {"valid": False, "error": f"Could not reach {provider} API — check your network"}
    except requests.exceptions.Timeout:
        return {"valid": False, "error": f"{provider} API timed out — try again"}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}
