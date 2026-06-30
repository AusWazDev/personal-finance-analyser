"""Pluggable AI backend — Claude API or Ollama (local).

Usage:
    from src.ai_backend import get_backend
    client = get_backend(config)
    resp = client.messages.create(model=..., max_tokens=..., messages=..., system=...)
    text = resp.content[0].text

Config keys:
    ai_backend:   "claude" (default) | "ollama"
    ollama_url:   "http://localhost:11434"  (Ollama default)
    ollama_model: "llama3"                  (Ollama default)
"""
import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


class _Content:
    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text


class _Response:
    """Minimal response object matching anthropic.types.Message shape."""

    __slots__ = ("content",)

    def __init__(self, text: str) -> None:
        self.content = [_Content(text)]


class _OllamaMessages:
    """Drop-in for anthropic.Anthropic().messages — calls the local Ollama API."""

    def __init__(self, url: str, model: str) -> None:
        self._url = url.rstrip("/")
        self._model = model

    def create(
        self,
        *,
        model: str,      # Claude model name — ignored, uses self._model
        max_tokens: int = 1024,
        messages: list,
        system: str | None = None,
        **_,
    ) -> _Response:
        full_messages: list = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)

        payload = json.dumps(
            {
                "model": self._model,
                "messages": full_messages,
                "stream": False,
                "options": {"num_predict": max_tokens},
            }
        ).encode()

        req = urllib.request.Request(
            f"{self._url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read())
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Ollama request failed: {exc}") from exc

        return _Response(result["message"]["content"])


class OllamaBackend:
    """AI backend using a locally-running Ollama instance (zero API cost)."""

    def __init__(self, config: dict) -> None:
        url = (config.get("ollama_url") or "http://localhost:11434")
        model = (config.get("ollama_model") or "llama3")
        self.messages = _OllamaMessages(url, model)
        logger.info(f"AI backend: Ollama ({url}, model={model})")


class ClaudeBackend:
    """AI backend using the Anthropic Claude API."""

    def __init__(self, config: dict) -> None:
        import anthropic
        api_key = config.get("anthropic_api_key") or None
        self._client = anthropic.Anthropic(**({"api_key": api_key} if api_key else {}))
        self.messages = self._client.messages
        logger.info("AI backend: Claude API")


def get_backend(config: dict) -> ClaudeBackend | OllamaBackend:
    """Return the configured AI backend.

    Reads config key ``ai_backend`` (default: "claude").
    Pass ``ai_backend: ollama`` to use a local Ollama instance instead.
    """
    name = (config.get("ai_backend") or "claude").lower().strip()
    if name == "ollama":
        return OllamaBackend(config)
    return ClaudeBackend(config)
