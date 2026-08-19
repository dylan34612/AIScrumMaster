"""Legacy Microsoft Graph delegated authentication via MSAL."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import msal

from agenticscrum.config import PROJECT_ROOT, Settings


TOKEN_CACHE_PATH = PROJECT_ROOT / "data" / "msal_token_cache.bin"
PUBLIC_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"


class GraphAuthenticator:
    """Acquire and cache Microsoft Graph access tokens."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.cache = msal.SerializableTokenCache()
        if TOKEN_CACHE_PATH.exists():
            self.cache.deserialize(TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
        scopes = self.settings.graph_auth_scopes
        if not settings.graph_client_id:
            needs_custom_client = [
                scope
                for scope in scopes
                if scope.startswith("Chat.") or scope.startswith("ChannelMessage.")
            ]
            if needs_custom_client:
                raise RuntimeError(
                    "GRAPH_CLIENT_ID is required to request Microsoft Graph scopes "
                    f"({', '.join(needs_custom_client)}). "
                    "Create/use an Entra ID app registration (public client) with delegated "
                    "Microsoft Graph permissions and set GRAPH_CLIENT_ID in `.env`. "
                    "Microsoft-owned public client IDs (like Azure CLI) will fail for these scopes "
                    "with AADSTS65002."
                )
        client_id = settings.graph_client_id or PUBLIC_CLIENT_ID
        authority = f"https://login.microsoftonline.com/{settings.graph_tenant_id}"
        self.app = msal.PublicClientApplication(
            client_id=client_id,
            authority=authority,
            token_cache=self.cache,
        )

    def acquire_token(self, interactive: bool = False, device_code: bool = False) -> str:
        """Acquire a Graph access token.

        - Non-interactive usage should be silent-only (no prompts).
        - Interactive usage is reserved for `agenticscrum init`.
        """

        result: dict[str, Any] | None = None
        scopes = self.settings.graph_auth_scopes
        accounts = self.app.get_accounts()
        if accounts:
            result = self.app.acquire_token_silent(scopes, account=accounts[0])
        if not result or "access_token" not in result:
            if not interactive:
                if device_code:
                    flow = self.app.initiate_device_flow(scopes=scopes)
                    if "user_code" not in flow:
                        raise RuntimeError(f"Failed to create device flow: {flow}")
                    print(flow["message"], flush=True)
                    result = self.app.acquire_token_by_device_flow(flow)
                else:
                    raise RuntimeError(
                        "No cached Graph token is available. Run `python -m agenticscrum init` "
                        "to sign in interactively and create the token cache."
                    )
            else:
                result = self.app.acquire_token_interactive(scopes=scopes)
        self._save_cache()
        if not result or "access_token" not in result:
            error = result.get("error_description") if result else "unknown error"
            if "AADSTS7000218" in error:
                raise RuntimeError(
                    "Failed to acquire Graph token: your GRAPH_CLIENT_ID is being treated as a "
                    "confidential client and requires a client secret. `agenticscrum init` uses "
                    "a public-client flow (interactive/device-code). Fix by enabling public client "
                    "flows on the app registration (Entra ID → App registrations → Authentication → "
                    "enable 'Allow public client flows' and add a 'Mobile and desktop applications' "
                    "platform redirect like http://localhost), or create a separate public-client "
                    "app registration for Graph and set GRAPH_CLIENT_ID to that."
                    f"\n\nUnderlying error: {error}"
                )
            if "AADSTS65001" in error:
                raise RuntimeError(
                    "Failed to acquire Graph token (consent required). "
                    "An Entra ID admin may need to grant consent for the requested delegated "
                    f"Microsoft Graph permissions: {', '.join(scopes)}. "
                    f"Underlying error: {error}"
                )
            raise RuntimeError(f"Failed to acquire Graph token: {error}")
        return str(result["access_token"])

    def _save_cache(self) -> None:
        if self.cache.has_state_changed:
            TOKEN_CACHE_PATH.write_text(self.cache.serialize(), encoding="utf-8")


def ensure_graph_login(settings: Settings, *, device_code: bool = False) -> None:
    """Prompt for Graph login and cache a refresh token."""

    if device_code:
        GraphAuthenticator(settings).acquire_token(interactive=False, device_code=True)
    else:
        GraphAuthenticator(settings).acquire_token(interactive=True)
