"""LangChain chat model factory for Agentic Scrum."""

from __future__ import annotations

from azure.identity import ClientSecretCredential, get_bearer_token_provider
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import AzureChatOpenAI, ChatOpenAI

from agenticscrum.config import Settings
from agenticscrum.llm.auth import build_llm_token_provider


def build_chat_model(settings: Settings) -> BaseChatModel:
    """Build a LangChain chat model for the configured OpenAI-compatible endpoint."""

    settings.require_llm()
    if settings.llm_auth_mode == "browser":
        return AzureChatOpenAI(
            azure_endpoint=settings.llm_api_base,
            azure_deployment=settings.llm_model,
            azure_ad_token_provider=build_llm_token_provider(settings),
            api_version=settings.llm_api_version,
            temperature=settings.llm_temperature,
        )

    if settings.llm_auth_mode == "azure_ad":
        credential = ClientSecretCredential(
            tenant_id=settings.llm_azure_tenant_id,
            client_id=settings.llm_azure_client_id,
            client_secret=settings.llm_azure_client_secret,
        )
        token_provider = get_bearer_token_provider(credential, settings.llm_azure_audience)
        return AzureChatOpenAI(
            azure_endpoint=settings.llm_api_base,
            azure_deployment=settings.llm_model,
            azure_ad_token_provider=token_provider,
            api_version=settings.llm_api_version,
            temperature=settings.llm_temperature,
        )

    return ChatOpenAI(
        model=settings.llm_model,
        base_url=settings.llm_api_base,
        api_key=settings.llm_api_key,
        temperature=settings.llm_temperature,
    )
