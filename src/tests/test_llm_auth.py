"""Tests for LLM auth modes including browser login helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agenticscrum.config import Settings
from agenticscrum.llm import auth as llm_auth
from agenticscrum.llm.client import build_chat_model


def _browser_settings(**overrides: object) -> Settings:
    values = {
        "llm_api_base": "https://example.openai.azure.com/",
        "llm_auth_mode": "browser",
        "llm_azure_tenant_id": "tenant-id",
        "llm_azure_user_client_id": "user-client-id",
        "llm_azure_user_audience": "api://user-client-id/.default",
        "llm_azure_client_id": "",
        "llm_azure_client_secret": "",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_browser_mode_does_not_require_client_secret() -> None:
    settings = _browser_settings()
    settings.require_llm()


def test_azure_ad_mode_requires_client_secret() -> None:
    settings = Settings(
        llm_api_base="https://example.openai.azure.com/",
        llm_auth_mode="azure_ad",
        llm_azure_tenant_id="tenant-id",
        llm_azure_client_id="",
        llm_azure_client_secret="",
        llm_azure_audience="api://sp-app/.default",
    )
    with pytest.raises(RuntimeError, match="LLM_AZURE_CLIENT"):
        settings.require_llm()


def test_browser_mode_requires_user_client_id() -> None:
    settings = _browser_settings(llm_azure_user_client_id="")
    with pytest.raises(RuntimeError, match="LLM_AZURE_USER_CLIENT_ID"):
        settings.require_llm()


def test_ensure_llm_login_rejects_non_browser_mode() -> None:
    settings = Settings(
        llm_api_base="https://example.openai.azure.com/",
        llm_auth_mode="api_key",
        llm_api_key="test-key",
    )
    with pytest.raises(RuntimeError, match="LLM_AUTH_MODE=browser"):
        llm_auth.ensure_llm_login(settings)


def test_build_chat_model_browser_uses_token_provider() -> None:
    settings = _browser_settings()
    provider = MagicMock(return_value="fake-token")

    with (
        patch("agenticscrum.llm.client.build_llm_token_provider", return_value=provider) as build_provider,
        patch("agenticscrum.llm.client.AzureChatOpenAI") as azure_cls,
    ):
        azure_cls.return_value = MagicMock(name="chat-model")
        model = build_chat_model(settings)

    build_provider.assert_called_once_with(settings)
    azure_cls.assert_called_once()
    kwargs = azure_cls.call_args.kwargs
    assert kwargs["azure_ad_token_provider"] is provider
    assert kwargs["azure_deployment"] == settings.llm_model
    assert model is azure_cls.return_value


def test_build_llm_token_provider_wraps_auth_errors(tmp_path, monkeypatch) -> None:
    settings = _browser_settings()
    monkeypatch.setattr(llm_auth, "AUTH_RECORD_PATH", tmp_path / "llm_auth_record.json")

    credential = MagicMock()
    with (
        patch.object(llm_auth, "build_llm_credential", return_value=credential),
        patch.object(
            llm_auth,
            "get_bearer_token_provider",
            return_value=MagicMock(side_effect=llm_auth.CredentialUnavailableError("no cache")),
        ),
    ):
        provider = llm_auth.build_llm_token_provider(settings)
        with pytest.raises(RuntimeError, match="agenticscrum login"):
            provider()


def test_auth_record_exists(tmp_path, monkeypatch) -> None:
    path = tmp_path / "llm_auth_record.json"
    monkeypatch.setattr(llm_auth, "AUTH_RECORD_PATH", path)
    assert llm_auth.auth_record_exists() is False
    path.write_text("{}", encoding="utf-8")
    assert llm_auth.auth_record_exists() is True
