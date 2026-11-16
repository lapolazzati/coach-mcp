"""U8: coach-mcp server and OAuth. GitHub is mocked; nothing leaves the process."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastmcp.server.auth.oauth_proxy.models import ClientCode
from starlette.testclient import TestClient

from coach.config import Settings, get_settings
from coach.db.migrate import connect, migrate
from coach.mcp import server as mcp_server
from coach.mcp.auth import (
    CLAUDE_REDIRECT_URI,
    DEFAULT_MAX_CLIENTS,
    RAYCAST_CLIENT_ID,
    AllowlistError,
    AllowlistGitHubTokenVerifier,
    CoachGitHubProvider,
    fetch_github_user_id,
)
from coach.mcp.server import StartupError, build_server

PUBLIC_URL = "https://coach-mcp.example.test"
RESOURCE = PUBLIC_URL + "/mcp"
LAPO_ID = 4242
OTHER_ID = 999

# Upstream GitHub tokens -> how the mocked api.github.com/user answers.
GITHUB_USERS: dict[str, tuple[int, Any]] = {
    "gh-lapo": (200, {"id": LAPO_ID, "login": "lapo"}),
    "gh-other": (200, {"id": OTHER_ID, "login": "intruder"}),
    "gh-error": (500, {"message": "boom"}),
    "gh-unauthorized": (401, {"message": "Bad credentials"}),
    "gh-noid": (200, {"login": "ghost"}),
    "gh-strid": (200, {"id": str(LAPO_ID)}),
    "gh-boolid": (200, {"id": True}),
}

MCP_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def github_handler(request: httpx.Request) -> httpx.Response:
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if token == "gh-down":
        raise httpx.ConnectError("unreachable", request=request)
    if request.url.path == "/user/repos":
        return httpx.Response(200, json=[], headers={"x-oauth-scopes": "user"})
    if request.url.path == "/user" and token in GITHUB_USERS:
        status, body = GITHUB_USERS[token]
        return httpx.Response(status, json=body)
    return httpx.Response(401, json={"message": "Bad credentials"})


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def github() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(github_handler))


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "coach.sqlite"
    conn = connect(path)
    migrate(conn)
    conn.close()
    return path


@pytest.fixture
def settings(db_path: Path) -> Settings:
    return Settings(
        coach_db_path=db_path,
        coach_mcp_public_url=PUBLIC_URL,
        coach_mcp_allowed_github_id=LAPO_ID,
        coach_mcp_github_client_id="gh-client-id",
        coach_mcp_github_client_secret="gh-client-secret",
        coach_mcp_jwt_key="j" * 48,
        coach_mcp_storage_key="s" * 48,
    )


@pytest.fixture
def make_client(
    settings: Settings, tmp_path: Path, github: httpx.AsyncClient
) -> Callable[..., tuple[TestClient, CoachGitHubProvider]]:
    clients: list[TestClient] = []

    def _make(
        storage_dir: Path | None = None,
    ) -> tuple[TestClient, CoachGitHubProvider]:
        mcp = build_server(
            settings,
            storage_dir=storage_dir or tmp_path / "mcp-auth",
            http_client=github,
        )
        assert isinstance(mcp.auth, CoachGitHubProvider)
        client = TestClient(mcp.http_app(path="/mcp"), base_url=PUBLIC_URL)
        client.__enter__()
        clients.append(client)
        return client, mcp.auth

    yield _make
    for client in clients:
        client.__exit__(None, None, None)


def register(
    client: TestClient, redirect_uri: str = CLAUDE_REDIRECT_URI
) -> httpx.Response:
    return client.post(
        "/register",
        json={
            "redirect_uris": [redirect_uri],
            "client_name": "Claude",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def _store_code(provider: CoachGitHubProvider, code: ClientCode) -> None:
    await provider._code_store.put(key=code.code, value=code, ttl=300)


def seed_code(
    client: TestClient,
    provider: CoachGitHubProvider,
    client_id: str,
    idp_tokens: dict[str, Any],
    redirect_uri: str = CLAUDE_REDIRECT_URI,
) -> tuple[str, str]:
    """Stand in for the GitHub callback: an authorization code holding upstream tokens."""
    verifier = secrets.token_urlsafe(48)
    code = ClientCode(
        code=secrets.token_urlsafe(24),
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=s256(verifier),
        code_challenge_method="S256",
        scopes=["user"],
        idp_tokens=idp_tokens,
        expires_at=time.time() + 300,
        created_at=time.time(),
    )
    portal = client.portal
    assert portal is not None
    portal.call(_store_code, provider, code)
    return code.code, verifier


def exchange(
    client: TestClient,
    client_id: str,
    code: str,
    verifier: str,
    redirect_uri: str = CLAUDE_REDIRECT_URI,
) -> httpx.Response:
    return client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
        },
    )


def login(
    client: TestClient, provider: CoachGitHubProvider, idp_tokens: dict[str, Any]
) -> tuple[str, httpx.Response]:
    client_id = register(client).json()["client_id"]
    code, verifier = seed_code(client, provider, client_id, idp_tokens)
    return client_id, exchange(client, client_id, code, verifier)


def call_mcp(client: TestClient, access_token: str | None) -> httpx.Response:
    headers = dict(MCP_HEADERS)
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return client.post("/mcp", json=MCP_INIT, headers=headers)


# --- discovery and the 401 challenge ------------------------------------------------


def test_unauthenticated_mcp_gets_401_with_resource_metadata(make_client) -> None:
    client, _ = make_client()
    response = call_mcp(client, None)
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer")
    assert (
        f'resource_metadata="{PUBLIC_URL}/.well-known/oauth-protected-resource/mcp"'
        in challenge
    )


def test_protected_resource_metadata_names_the_mcp_url(make_client) -> None:
    client, _ = make_client()
    meta = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert meta["resource"] == RESOURCE
    assert [s.rstrip("/") for s in meta["authorization_servers"]] == [PUBLIC_URL]


def test_authorization_server_metadata_advertises_s256_and_endpoints(
    make_client,
) -> None:
    client, _ = make_client()
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["issuer"].rstrip("/") == PUBLIC_URL
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["authorization_endpoint"] == f"{PUBLIC_URL}/authorize"
    assert meta["token_endpoint"] == f"{PUBLIC_URL}/token"
    assert meta["registration_endpoint"] == f"{PUBLIC_URL}/register"


def test_healthz_reports_schema_version(make_client) -> None:
    client, _ = make_client()
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "schema_version": 6}


# --- DCR hardening ------------------------------------------------------------------


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "https://evil.example/api/mcp/auth_callback",
        "https://claude.ai/api/mcp/auth_callback/../../steal",
        "https://claude.ai/other",
        "http://claude.ai/api/mcp/auth_callback",
        "http://localhost:3000/callback",
    ],
)
def test_dcr_rejects_other_redirect_uris(make_client, redirect_uri: str) -> None:
    client, _ = make_client()
    response = register(client, redirect_uri)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


def test_dcr_accepts_claude_callback(make_client) -> None:
    client, _ = make_client()
    response = register(client)
    assert response.status_code == 201
    assert response.json()["redirect_uris"] == [CLAUDE_REDIRECT_URI]


def test_dcr_rejects_registrations_past_the_cap(make_client, tmp_path: Path) -> None:
    client, _ = make_client()
    for _ in range(DEFAULT_MAX_CLIENTS):
        assert register(client).status_code == 201
    response = register(client)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_client_metadata"
    assert "limit" in response.json()["error_description"]
    # The count lives in the encrypted store, so a restart does not reset it.
    restarted, _ = make_client(tmp_path / "mcp-auth")
    assert register(restarted).status_code == 400


# --- PKCE ---------------------------------------------------------------------------


def _authorize(client: TestClient, client_id: str, method: str) -> httpx.Response:
    return client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CLAUDE_REDIRECT_URI,
            "code_challenge": s256("v" * 50) if method == "S256" else "v" * 50,
            "code_challenge_method": method,
            "state": "xyz",
            "resource": RESOURCE,
        },
        follow_redirects=False,
    )


def test_plain_pkce_is_rejected(make_client) -> None:
    client, _ = make_client()
    client_id = register(client).json()["client_id"]
    response = _authorize(client, client_id, "plain")
    # Valid client + redirect_uri: the error goes back to Claude's callback.
    assert response.status_code == 302
    location = urlparse(response.headers["location"])
    assert (
        f"{location.scheme}://{location.netloc}{location.path}" == CLAUDE_REDIRECT_URI
    )
    assert parse_qs(location.query)["error"] == ["invalid_request"]


def test_s256_pkce_proceeds_to_consent(make_client) -> None:
    client, _ = make_client()
    client_id = register(client).json()["client_id"]
    response = _authorize(client, client_id, "S256")
    assert response.status_code == 302
    assert "error=" not in response.headers["location"]
    assert not response.headers["location"].startswith(CLAUDE_REDIRECT_URI)


# --- Raycast (pinned CIMD client) ---------------------------------------------------

RAYCAST_LOOPBACK = "http://127.0.0.1:58866/callback"


def _authorize_raycast(client: TestClient, redirect_uri: str) -> httpx.Response:
    return client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": RAYCAST_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "code_challenge": s256("v" * 50),
            "code_challenge_method": "S256",
            "state": "xyz",
            "resource": RESOURCE,
        },
        follow_redirects=False,
    )


def test_raycast_loopback_proceeds_to_consent(make_client) -> None:
    client, _ = make_client()
    response = _authorize_raycast(client, RAYCAST_LOOPBACK)
    assert response.status_code == 302
    assert "error=" not in response.headers["location"]
    assert not response.headers["location"].startswith("http://127.0.0.1")


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "https://evil.example/callback",
        "http://127.0.0.1:58866/other",
        "http://localhost:58866/callback",
        "http://127.0.0.1.evil.example:58866/callback",
        "com.raycast:/oauth",
        CLAUDE_REDIRECT_URI,
    ],
)
def test_raycast_rejects_non_loopback_redirects(make_client, redirect_uri: str) -> None:
    client, _ = make_client()
    response = _authorize_raycast(client, redirect_uri)
    assert response.status_code == 400


def test_raycast_idp_callback_is_not_blocked(make_client) -> None:
    """GitHub's callback re-checks the stored redirect; it must accept Raycast's."""
    import re

    client, _ = make_client()
    consent = _authorize_raycast(client, RAYCAST_LOOPBACK)
    page = client.get(consent.headers["location"])
    assert page.status_code == 200
    fields = dict(
        re.findall(r'name="(txn_id|csrf_token)"\s+value="([^"]+)"', page.text)
    )
    assert set(fields) == {"txn_id", "csrf_token"}, page.text[:500]
    approved = client.post(
        "/consent",
        data={**fields, "action": "approve"},
        follow_redirects=False,
    )
    assert approved.status_code == 302, approved.text
    upstream = urlparse(approved.headers["location"])
    state = parse_qs(upstream.query)["state"][0]
    callback = client.get(
        "/auth/callback", params={"code": "c", "state": state}, follow_redirects=False
    )
    assert "Invalid redirect URI" not in callback.text


def test_idp_callback_still_blocks_unknown_redirects(make_client) -> None:
    _, provider = make_client()
    assert provider._validate_client_redirect_uri(RAYCAST_LOOPBACK)
    assert provider._validate_client_redirect_uri(CLAUDE_REDIRECT_URI)
    assert not provider._validate_client_redirect_uri("https://evil.example/callback")
    assert not provider._validate_client_redirect_uri("http://localhost:58866/callback")


def test_raycast_still_goes_through_the_allowlist(make_client) -> None:
    client, provider = make_client()
    code, verifier = seed_code(
        client,
        provider,
        RAYCAST_CLIENT_ID,
        {"access_token": "gh-other"},
        RAYCAST_LOOPBACK,
    )
    response = exchange(client, RAYCAST_CLIENT_ID, code, verifier, RAYCAST_LOOPBACK)
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_grant"


def test_raycast_token_exchange_for_lapo(make_client) -> None:
    client, provider = make_client()
    code, verifier = seed_code(
        client,
        provider,
        RAYCAST_CLIENT_ID,
        {"access_token": "gh-lapo"},
        RAYCAST_LOOPBACK,
    )
    response = exchange(client, RAYCAST_CLIENT_ID, code, verifier, RAYCAST_LOOPBACK)
    assert response.status_code == 200, response.text
    assert call_mcp(client, response.json()["access_token"]).status_code == 200


# --- the one-account allowlist -------------------------------------------------------


@pytest.mark.parametrize(
    "upstream_token",
    [
        "gh-other",
        "gh-error",
        "gh-unauthorized",
        "gh-noid",
        "gh-strid",
        "gh-boolid",
        "gh-down",
    ],
)
def test_allowlist_refuses_at_token_exchange(make_client, upstream_token: str) -> None:
    client, provider = make_client()
    client_id = register(client).json()["client_id"]
    code, verifier = seed_code(
        client,
        provider,
        client_id,
        {"access_token": upstream_token, "token_type": "bearer"},
    )
    response = exchange(client, client_id, code, verifier)
    # FastMCP's TokenHandler turns invalid_grant into 401 (MCP spec).
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_grant"
    assert "access_token" not in response.json()
    # The refused code is burnt, so it cannot be retried.
    retry = exchange(client, client_id, code, verifier)
    assert retry.status_code == 401
    assert retry.json()["error"] == "invalid_grant"


def test_allowlist_accepts_lapo_and_mcp_call_succeeds(make_client) -> None:
    client, provider = make_client()
    _, response = login(
        client, provider, {"access_token": "gh-lapo", "token_type": "bearer"}
    )
    assert response.status_code == 200, response.text
    tokens = response.json()
    # GitHub OAuth App tokens never expire and carry no refresh token; the coach
    # token is still capped at about an hour.
    assert tokens["expires_in"] == 3600
    assert call_mcp(client, tokens["access_token"]).status_code == 200


def test_access_token_audience_is_the_mcp_url(make_client) -> None:
    client, provider = make_client()
    _, response = login(
        client, provider, {"access_token": "gh-lapo", "token_type": "bearer"}
    )
    payload_b64 = response.json()["access_token"].split(".")[1]
    payload = json.loads(
        base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
    )
    assert payload["aud"] == RESOURCE
    assert payload["iss"].rstrip("/") == PUBLIC_URL
    assert payload["exp"] - payload["iat"] == 3600


@pytest.mark.anyio
async def test_fetch_github_user_id_fails_closed(github: httpx.AsyncClient) -> None:
    assert await fetch_github_user_id("gh-lapo", github) == LAPO_ID
    for token in ("gh-error", "gh-noid", "gh-strid", "gh-boolid", "gh-down", "nope"):
        with pytest.raises(AllowlistError):
            await fetch_github_user_id(token, github)


@pytest.mark.anyio
async def test_per_request_verifier_only_accepts_lapo(
    github: httpx.AsyncClient,
) -> None:
    verifier = AllowlistGitHubTokenVerifier(
        allowed_github_id=LAPO_ID, required_scopes=["user"], http_client=github
    )
    assert await verifier.verify_token("gh-lapo") is not None
    assert await verifier.verify_token("gh-other") is None
    assert await verifier.verify_token("gh-noid") is None
    assert await verifier.verify_token("gh-error") is None


# --- refresh tokens (GitHub App with expiring user tokens) ---------------------------


GITHUB_APP_TOKENS = {
    "access_token": "gh-lapo",
    "token_type": "bearer",
    "expires_in": 28800,
    "refresh_token": "ghr-1",
    "refresh_token_expires_in": 15811200,
}


def _mock_upstream_refresh(provider: CoachGitHubProvider) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "github.com"
        return httpx.Response(
            200,
            json={
                "access_token": "gh-lapo",
                "token_type": "bearer",
                "expires_in": 28800,
                "refresh_token": "ghr-2",
                "refresh_token_expires_in": 15811200,
            },
        )

    def factory() -> AsyncOAuth2Client:
        return AsyncOAuth2Client(
            client_id="gh-client-id",
            client_secret="gh-client-secret",
            transport=httpx.MockTransport(handler),
        )

    provider._create_upstream_oauth_client = factory  # type: ignore[method-assign]


def test_refresh_token_rotates_and_old_one_is_invalid_grant(make_client) -> None:
    client, provider = make_client()
    client_id, response = login(client, provider, GITHUB_APP_TOKENS)
    first = response.json()
    assert first["expires_in"] == 3600
    assert first["refresh_token"]
    _mock_upstream_refresh(provider)

    refreshed = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": client_id,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    second = refreshed.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert second["expires_in"] == 3600

    replay = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": client_id,
        },
    )
    assert replay.status_code == 401
    assert replay.json()["error"] == "invalid_grant"


def test_refresh_token_lifetime_is_bounded(make_client) -> None:
    client, provider = make_client()
    _, response = login(client, provider, GITHUB_APP_TOKENS)
    refresh = response.json()["refresh_token"]
    payload_b64 = refresh.split(".")[1]
    payload = json.loads(
        base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
    )
    assert payload["exp"] - payload["iat"] == 30 * 24 * 3600


def test_garbage_refresh_token_is_invalid_grant(make_client) -> None:
    client, _ = make_client()
    client_id = register(client).json()["client_id"]
    response = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": "not-a-token",
            "client_id": client_id,
        },
    )
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_grant"


# --- persistence --------------------------------------------------------------------


def test_token_survives_restart(make_client, tmp_path: Path) -> None:
    storage = tmp_path / "mcp-auth"
    first, provider = make_client(storage)
    _, response = login(
        first, provider, {"access_token": "gh-lapo", "token_type": "bearer"}
    )
    token = response.json()["access_token"]
    assert call_mcp(first, token).status_code == 200

    restarted, _ = make_client(storage)
    assert call_mcp(restarted, token).status_code == 200

    # Control: a server with empty storage does not know the token.
    fresh, _ = make_client(tmp_path / "other-auth")
    assert call_mcp(fresh, token).status_code == 401


def test_storage_is_encrypted_on_disk(make_client, tmp_path: Path) -> None:
    storage = tmp_path / "mcp-auth"
    client, provider = make_client(storage)
    login(client, provider, {"access_token": "gh-lapo", "token_type": "bearer"})
    blobs = b"".join(p.read_bytes() for p in storage.rglob("*") if p.is_file())
    assert blobs
    assert b"gh-lapo" not in blobs
    assert CLAUDE_REDIRECT_URI.encode() not in blobs


# --- refusing to start ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "env"),
    [
        ("coach_mcp_jwt_key", "COACH_MCP_JWT_KEY"),
        ("coach_mcp_storage_key", "COACH_MCP_STORAGE_KEY"),
        ("coach_mcp_public_url", "COACH_MCP_PUBLIC_URL"),
        ("coach_mcp_allowed_github_id", "COACH_MCP_ALLOWED_GITHUB_ID"),
        ("coach_mcp_github_client_id", "COACH_MCP_GITHUB_CLIENT_ID"),
        ("coach_mcp_github_client_secret", "COACH_MCP_GITHUB_CLIENT_SECRET"),
    ],
)
def test_missing_setting_refuses_to_start(
    settings: Settings, tmp_path: Path, field: str, env: str
) -> None:
    incomplete = settings.model_copy(update={field: None})
    with pytest.raises(StartupError, match=env):
        build_server(incomplete, storage_dir=tmp_path / "mcp-auth")


def test_missing_jwt_key_exits_with_clear_message(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, tmp_path: Path, capsys
) -> None:
    for key, value in {
        "COACH_DB_PATH": str(db_path),
        "COACH_MCP_PUBLIC_URL": PUBLIC_URL,
        "COACH_MCP_ALLOWED_GITHUB_ID": str(LAPO_ID),
        "COACH_MCP_GITHUB_CLIENT_ID": "gh-client-id",
        "COACH_MCP_GITHUB_CLIENT_SECRET": "gh-client-secret",
        "COACH_MCP_STORAGE_KEY": "s" * 48,
        "COACH_MCP_JWT_KEY": "",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as exc_info:
            mcp_server.main(["--storage-dir", str(tmp_path / "mcp-auth")])
    finally:
        get_settings.cache_clear()
    assert exc_info.value.code == 2
    assert "COACH_MCP_JWT_KEY" in capsys.readouterr().err


def test_short_key_refuses_to_start(settings: Settings, tmp_path: Path) -> None:
    weak = settings.model_copy(
        update={"coach_mcp_jwt_key": type(settings.coach_mcp_jwt_key)("short")}
    )
    with pytest.raises(ValueError, match="COACH_MCP_JWT_KEY"):
        build_server(weak, storage_dir=tmp_path / "mcp-auth")


def test_http_public_url_refuses_to_start(settings: Settings, tmp_path: Path) -> None:
    insecure = settings.model_copy(
        update={"coach_mcp_public_url": "http://coach-mcp.example.test"}
    )
    with pytest.raises(StartupError, match="https"):
        build_server(insecure, storage_dir=tmp_path / "mcp-auth")


def test_old_schema_refuses_to_start(
    settings: Settings, db_path: Path, tmp_path: Path
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM schema_migrations WHERE version >= 4")
    conn.commit()
    conn.close()
    with pytest.raises(StartupError, match="schema version 3"):
        build_server(settings, storage_dir=tmp_path / "mcp-auth")


def test_missing_database_refuses_to_start(settings: Settings, tmp_path: Path) -> None:
    absent = settings.model_copy(update={"coach_db_path": tmp_path / "nope.sqlite"})
    with pytest.raises(StartupError, match="schema version 0"):
        build_server(absent, storage_dir=tmp_path / "mcp-auth")
