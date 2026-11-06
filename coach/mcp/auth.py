"""OAuth for coach-mcp: FastMCP's OAuthProxy in front of GitHub, locked to one account.

FastMCP 3.4 implements the OAuth server Claude's connector needs (discovery, 401 with
WWW-Authenticate, PKCE S256, DCR/CIMD, rotating refresh tokens). This module adds what
it lacks natively, each as the smallest override:

- One-account allowlist. ``CoachGitHubProvider.exchange_authorization_code`` asks
  GitHub ``/user`` for the numeric id behind the freshly exchanged upstream token and
  refuses to issue a coach token unless it equals ``COACH_MCP_ALLOWED_GITHUB_ID``.
  It fails closed: a GitHub error, bad JSON or a missing/non-integer id is a refusal.
  ``AllowlistGitHubTokenVerifier`` repeats the check on every MCP request.
- A cap on DCR registrations. ``OAuthProxy.register_client`` validates redirect_uris
  but never counts clients, so the override keeps its own registry and refuses
  registrations past the cap.

The redirect_uri restriction, S256-only PKCE, token lifetimes, audience and encrypted
disk storage use native FastMCP / MCP SDK settings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import httpx
from cryptography.fernet import Fernet
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from fastmcp.server.auth.providers.github import GitHubProvider, GitHubTokenVerifier
from key_value.aio.protocols import AsyncKeyValue
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from mcp.server.auth.provider import AuthorizationCode, RegistrationError, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

GITHUB_USER_URL = "https://api.github.com/user"
CLAUDE_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
DEFAULT_STORAGE_DIR = Path("/data/mcp-auth")
ACCESS_TOKEN_TTL_SECONDS = 3600  # ~1 h, per KTD2
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600  # absolute bound; rotation keeps it
DEFAULT_MAX_CLIENTS = 10
MIN_KEY_LENGTH = 32

_DCR_REGISTRY_COLLECTION = "coach-dcr-registry"
_DCR_REGISTRY_KEY = "clients"


class AllowlistError(Exception):
    """The GitHub identity could not be confirmed as the allowed account."""


async def fetch_github_user_id(token: str, client: httpx.AsyncClient) -> int:
    """Return the numeric GitHub id behind ``token``. Raises AllowlistError otherwise."""
    try:
        response = await client.get(
            GITHUB_USER_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "coach-mcp",
            },
        )
    except httpx.HTTPError as exc:
        raise AllowlistError(
            f"GitHub /user request failed: {type(exc).__name__}"
        ) from exc
    if response.status_code != 200:
        raise AllowlistError(f"GitHub /user returned HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise AllowlistError("GitHub /user returned invalid JSON") from exc
    user_id = data.get("id") if isinstance(data, dict) else None
    # bool is an int subclass; a JSON true must not pass as id 1.
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        raise AllowlistError("GitHub /user response has no numeric id")
    return user_id


async def check_allowed(token: str, allowed_id: int, client: httpx.AsyncClient) -> int:
    """Return the GitHub id if it is the allowed one. Raises AllowlistError otherwise."""
    user_id = await fetch_github_user_id(token, client)
    if user_id != allowed_id:
        raise AllowlistError(f"GitHub account {user_id} is not allowed")
    return user_id


class AllowlistGitHubTokenVerifier(GitHubTokenVerifier):
    """GitHubTokenVerifier that also rejects every GitHub account but the allowed one."""

    def __init__(self, *, allowed_github_id: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._allowed_github_id = allowed_github_id

    async def verify_token(self, token: str) -> AccessToken | None:
        result = await super().verify_token(token)
        if result is None:
            return None
        if result.claims.get("sub") != str(self._allowed_github_id):
            return None
        return result


def build_encrypted_storage(storage_dir: Path, storage_key: str) -> AsyncKeyValue:
    """Fernet-encrypted file store, same layout FastMCP uses for its default storage."""
    storage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fernet_key = derive_jwt_key(
        high_entropy_material=storage_key, salt="coach-mcp-storage-encryption-key"
    )
    file_store = FileTreeStore(
        data_directory=storage_dir,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(storage_dir),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(
            storage_dir
        ),
    )
    # Undecryptable entries (key rotated) read as missing: clients just re-register.
    return FernetEncryptionWrapper(
        key_value=file_store,
        fernet=Fernet(key=fernet_key),
        raise_on_decryption_error=False,
    )


class CoachGitHubProvider(GitHubProvider):
    """GitHubProvider with the one-account allowlist and a DCR client cap."""

    def __init__(
        self,
        *,
        public_url: str,
        github_client_id: str,
        github_client_secret: str,
        allowed_github_id: int,
        jwt_key: str,
        storage_key: str,
        storage_dir: Path = DEFAULT_STORAGE_DIR,
        max_clients: int = DEFAULT_MAX_CLIENTS,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        for name, value in (
            ("COACH_MCP_JWT_KEY", jwt_key),
            ("COACH_MCP_STORAGE_KEY", storage_key),
        ):
            if len(value) < MIN_KEY_LENGTH:
                raise ValueError(f"{name} must be at least {MIN_KEY_LENGTH} characters")
        public_url = public_url.rstrip("/")
        super().__init__(
            client_id=github_client_id,
            client_secret=github_client_secret,
            # base_url == issuer == origin; resource (and token audience) become
            # origin + "/mcp" once FastMCP calls set_mcp_path("/mcp").
            base_url=public_url,
            issuer_url=public_url,
            allowed_client_redirect_uris=[CLAUDE_REDIRECT_URI],
            client_storage=build_encrypted_storage(storage_dir, storage_key),
            # bytes skip FastMCP's PBKDF2 path; the env key is already high entropy.
            jwt_signing_key=derive_jwt_key(
                high_entropy_material=jwt_key, salt="coach-mcp-jwt"
            ),
            fastmcp_access_token_expiry_seconds=ACCESS_TOKEN_TTL_SECONDS,
            fallback_refresh_token_expiry_seconds=REFRESH_TOKEN_TTL_SECONDS,
            http_client=http_client,
        )
        self._allowed_github_id = allowed_github_id
        self._max_clients = max_clients
        self._http_client = http_client
        self._registry_lock = anyio.Lock()
        # GitHubProvider builds its own verifier and takes no token_verifier argument,
        # so swap in the allowlisting subclass with the same settings.
        self._token_validator = AllowlistGitHubTokenVerifier(
            allowed_github_id=allowed_github_id,
            required_scopes=self._token_validator.required_scopes,
            cache_ttl_seconds=300,
            http_client=http_client,
        )

    async def _check_github_identity(self, upstream_access_token: str) -> None:
        if self._http_client is not None:
            await check_allowed(
                upstream_access_token, self._allowed_github_id, self._http_client
            )
            return
        async with httpx.AsyncClient(timeout=10) as client:
            await check_allowed(upstream_access_token, self._allowed_github_id, client)

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        # Runs after the GitHub token exchange (the upstream tokens sit on the stored
        # code) and before FastMCP issues any coach token.
        code_model = await self._code_store.get(key=authorization_code.code)
        if code_model is None:
            raise TokenError("invalid_grant", "Authorization code not found")
        upstream_token = code_model.idp_tokens.get("access_token")
        try:
            if not isinstance(upstream_token, str) or not upstream_token:
                raise AllowlistError("No upstream GitHub access token")
            await self._check_github_identity(upstream_token)
        except AllowlistError as exc:
            await self._code_store.delete(key=authorization_code.code)
            raise TokenError(
                "invalid_grant", "This GitHub account is not allowed"
            ) from exc
        return await super().exchange_authorization_code(client, authorization_code)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        async with self._registry_lock:
            registry = await self._client_storage.get(
                key=_DCR_REGISTRY_KEY, collection=_DCR_REGISTRY_COLLECTION
            )
            client_ids = list(registry.get("client_ids", [])) if registry else []
            if len(client_ids) >= self._max_clients:
                raise RegistrationError(
                    "invalid_client_metadata",
                    f"Client registration limit ({self._max_clients}) reached",
                )
            # Validates redirect_uris against CLAUDE_REDIRECT_URI and stores the client.
            await super().register_client(client_info)
            client_ids.append(client_info.client_id)
            await self._client_storage.put(
                key=_DCR_REGISTRY_KEY,
                value={"client_ids": client_ids},
                collection=_DCR_REGISTRY_COLLECTION,
            )
