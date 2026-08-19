"""Interactive browser authentication for Entra-backed OpenAI-compatible proxies."""

from __future__ import annotations

from collections.abc import Callable

from azure.core.exceptions import ClientAuthenticationError
from azure.identity import (
    AuthenticationRecord,
    CredentialUnavailableError,
    InteractiveBrowserCredential,
    TokenCachePersistenceOptions,
    get_bearer_token_provider,
)

from agenticscrum.config import PROJECT_ROOT, Settings

AUTH_RECORD_PATH = PROJECT_ROOT / "data" / "llm_auth_record.json"
TOKEN_CACHE_NAME = "agenticscrum-llm"


def auth_record_exists() -> bool:
    """Return whether a persisted LLM authentication record is present."""

    return AUTH_RECORD_PATH.exists()


def _cache_options() -> TokenCachePersistenceOptions:
    return TokenCachePersistenceOptions(
        name=TOKEN_CACHE_NAME,
        allow_unencrypted_storage=True,
    )


def _load_auth_record() -> AuthenticationRecord | None:
    if not AUTH_RECORD_PATH.exists():
        return None
    return AuthenticationRecord.deserialize(AUTH_RECORD_PATH.read_text(encoding="utf-8"))


def _save_auth_record(record: AuthenticationRecord) -> None:
    AUTH_RECORD_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUTH_RECORD_PATH.write_text(record.serialize(), encoding="utf-8")


def build_llm_credential(
    settings: Settings,
    *,
    interactive: bool,
) -> InteractiveBrowserCredential:
    """Build an InteractiveBrowserCredential for LLM proxy access.

    When ``interactive`` is False, automatic browser prompts are disabled so
    scheduled jobs never hang waiting for a human. Call ``ensure_llm_login``
    (or ``python -m agenticscrum login``) to create the cache first.
    """

    settings.require_llm()
    if settings.llm_auth_mode != "browser":
        raise RuntimeError(
            "build_llm_credential requires LLM_AUTH_MODE=browser. "
            f"Current mode: {settings.llm_auth_mode}."
        )

    record = _load_auth_record()
    kwargs: dict = {
        "client_id": settings.llm_azure_user_client_id,
        "tenant_id": settings.llm_azure_tenant_id,
        "cache_persistence_options": _cache_options(),
        "disable_automatic_authentication": not interactive,
    }
    if record is not None:
        kwargs["authentication_record"] = record
    return InteractiveBrowserCredential(**kwargs)


def ensure_llm_login(settings: Settings) -> AuthenticationRecord:
    """Open a browser login, persist the auth record, and return it."""

    settings.require_llm()
    if settings.llm_auth_mode != "browser":
        raise RuntimeError(
            "LLM login is only used when LLM_AUTH_MODE=browser. "
            f"Current mode: {settings.llm_auth_mode}."
        )

    credential = build_llm_credential(settings, interactive=True)
    record = credential.authenticate(scopes=[settings.llm_azure_user_audience])
    _save_auth_record(record)
    # Touch the token cache so a refresh token is stored for silent reuse.
    credential.get_token(settings.llm_azure_user_audience)
    return record


def build_llm_token_provider(settings: Settings) -> Callable[[], str]:
    """Return a silent-only bearer token provider for AzureChatOpenAI."""

    credential = build_llm_credential(settings, interactive=False)
    raw_provider = get_bearer_token_provider(
        credential, settings.llm_azure_user_audience
    )

    def token_provider() -> str:
        try:
            return raw_provider()
        except (CredentialUnavailableError, ClientAuthenticationError) as exc:
            raise RuntimeError(
                "No usable LLM browser token is available. "
                "Run `python -m agenticscrum login` to sign in interactively, "
                "then retry."
            ) from exc

    return token_provider


def probe_llm_browser_token(settings: Settings) -> str:
    """Attempt a silent token acquire; return a short status message."""

    if not auth_record_exists():
        return "LLM browser auth record: missing (run `python -m agenticscrum login`)"
    try:
        token = build_llm_token_provider(settings)()
        if token:
            return "LLM browser token: acquired (silent)"
        return "LLM browser token: empty response"
    except Exception as exc:  # noqa: BLE001 - doctor should never crash
        return f"LLM browser token: failed ({exc})"
