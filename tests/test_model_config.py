from __future__ import annotations

from types import SimpleNamespace

import pytest

from nekro_plugin_media import gemini, model_config


def test_openai_endpoint_is_derived_from_model_group_base_url() -> None:
    assert model_config._is_native_gemini_url("https://generativelanguage.googleapis.com")
    assert not model_config._is_native_gemini_url("https://api.example.test/v1")
    assert gemini._chat_completions_url("https://api.example.test/v1") == "https://api.example.test/v1/chat/completions"
    assert gemini._chat_completions_url("https://api.example.test") == "https://api.example.test/v1/chat/completions"


def test_resolve_model_group_uses_group_values(monkeypatch: pytest.MonkeyPatch) -> None:
    group = SimpleNamespace(
        CHAT_MODEL="media-model",
        API_KEY="group-key",
        BASE_URL="https://api.example.test/v1",
        CHAT_PROXY="http://proxy.example.test:8080",
        TEMPERATURE=0.2,
        TOP_P=0.8,
        TOP_K=20,
        EXTRA_BODY='{"response_format":{"type":"text"}}',
    )
    monkeypatch.setattr(model_config, "_get_model_group_info", lambda _: group)

    resolved = model_config.resolve_model_config(SimpleNamespace(MEDIA_MODEL_GROUP="media", GEMINI_API_KEY="legacy"))

    assert resolved.model == "media-model"
    assert resolved.api_key == "group-key"
    assert resolved.base_url == "https://api.example.test/v1"
    assert not resolved.native_gemini


def test_legacy_gemini_config_is_used_without_model_group(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = model_config.resolve_model_config(
        SimpleNamespace(MEDIA_MODEL_GROUP="", GEMINI_API_KEY="legacy-key", GEMINI_MODEL="legacy-model")
    )

    assert resolved.model == "legacy-model"
    assert resolved.api_key == "legacy-key"
    assert resolved.native_gemini
