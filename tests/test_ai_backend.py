"""Tests for src/ai_backend.py."""
import json
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.ai_backend import (
    ClaudeBackend,
    OllamaBackend,
    _Content,
    _OllamaMessages,
    _Response,
    get_backend,
)


# ── _Response / _Content shape ────────────────────────────────────────────────

def test_response_content_text():
    r = _Response("hello world")
    assert r.content[0].text == "hello world"


def test_response_content_is_list():
    r = _Response("x")
    assert isinstance(r.content, list)
    assert len(r.content) == 1


def test_content_text_attribute():
    c = _Content("test")
    assert c.text == "test"


# ── get_backend routing ───────────────────────────────────────────────────────

def test_get_backend_defaults_to_claude():
    b = get_backend({})
    assert isinstance(b, ClaudeBackend)


def test_get_backend_explicit_claude():
    b = get_backend({"ai_backend": "claude"})
    assert isinstance(b, ClaudeBackend)


def test_get_backend_ollama():
    b = get_backend({"ai_backend": "ollama"})
    assert isinstance(b, OllamaBackend)


def test_get_backend_case_insensitive():
    b = get_backend({"ai_backend": "OLLAMA"})
    assert isinstance(b, OllamaBackend)


def test_get_backend_unknown_value_defaults_to_claude():
    b = get_backend({"ai_backend": "groq"})
    assert isinstance(b, ClaudeBackend)


# ── ClaudeBackend ─────────────────────────────────────────────────────────────

def test_claude_backend_has_messages_attribute():
    b = ClaudeBackend({})
    assert hasattr(b, "messages")


def test_claude_backend_messages_is_anthropic_messages_object():
    b = ClaudeBackend({})
    assert hasattr(b.messages, "create")


# ── OllamaBackend config ──────────────────────────────────────────────────────

def test_ollama_backend_default_url_and_model():
    b = OllamaBackend({})
    assert b.messages._url == "http://localhost:11434"
    assert b.messages._model == "llama3"


def test_ollama_backend_custom_url_and_model():
    b = OllamaBackend({"ollama_url": "http://gpu-box:11434", "ollama_model": "mistral"})
    assert b.messages._url == "http://gpu-box:11434"
    assert b.messages._model == "mistral"


def test_ollama_backend_strips_trailing_slash():
    b = OllamaBackend({"ollama_url": "http://localhost:11434/"})
    assert b.messages._url == "http://localhost:11434"


# ── _OllamaMessages.create ────────────────────────────────────────────────────

def _fake_resp(text: str):
    """Context-manager mock that returns Ollama JSON body."""
    mock = MagicMock()
    mock.read.return_value = json.dumps({"message": {"content": text}}).encode()
    mock.__enter__ = lambda self: self
    mock.__exit__ = MagicMock(return_value=False)
    return mock


def test_ollama_create_returns_response_shape():
    msgs = _OllamaMessages("http://localhost:11434", "llama3")
    with patch("urllib.request.urlopen", return_value=_fake_resp("Groceries")):
        result = msgs.create(model="m", max_tokens=100, messages=[{"role": "user", "content": "q"}])
    assert result.content[0].text == "Groceries"


def test_ollama_create_prepends_system_prompt():
    msgs = _OllamaMessages("http://localhost:11434", "llama3")
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        return _fake_resp("ok")

    with patch("urllib.request.urlopen", fake_urlopen):
        msgs.create(
            model="m",
            max_tokens=100,
            messages=[{"role": "user", "content": "q"}],
            system="SYS",
        )

    sent = captured["body"]["messages"]
    assert sent[0] == {"role": "system", "content": "SYS"}
    assert sent[1] == {"role": "user", "content": "q"}


def test_ollama_create_no_system_omits_system_message():
    msgs = _OllamaMessages("http://localhost:11434", "llama3")
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        return _fake_resp("ok")

    with patch("urllib.request.urlopen", fake_urlopen):
        msgs.create(model="m", max_tokens=50, messages=[{"role": "user", "content": "q"}])

    assert captured["body"]["messages"] == [{"role": "user", "content": "q"}]


def test_ollama_create_uses_config_model_not_claude_model():
    msgs = _OllamaMessages("http://localhost:11434", "mistral")
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        return _fake_resp("ok")

    with patch("urllib.request.urlopen", fake_urlopen):
        msgs.create(model="claude-haiku-4-5-20251001", max_tokens=100, messages=[])

    assert captured["body"]["model"] == "mistral"


def test_ollama_create_passes_max_tokens_as_num_predict():
    msgs = _OllamaMessages("http://localhost:11434", "llama3")
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        return _fake_resp("ok")

    with patch("urllib.request.urlopen", fake_urlopen):
        msgs.create(model="m", max_tokens=512, messages=[])

    assert captured["body"]["options"]["num_predict"] == 512


def test_ollama_create_connection_error_raises_runtime_error():
    msgs = _OllamaMessages("http://localhost:11434", "llama3")
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("Connection refused"),
    ):
        with pytest.raises(RuntimeError, match="Ollama request failed"):
            msgs.create(model="m", max_tokens=100, messages=[])
