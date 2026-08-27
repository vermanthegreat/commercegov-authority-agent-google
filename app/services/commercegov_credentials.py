import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import httpx


REQUIRED_SCOPE = "proposals:write"


class OAuthRefreshError(Exception):
    """A safe, token-free OAuth refresh failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class TokenStore(Protocol):
    async def access_latest(self, secret_name: str) -> str: ...

    async def add_version(self, secret_name: str, value: str) -> None: ...


class SecretManagerTokenStore:
    def __init__(self, project_id: str, client: Any | None = None):
        self.project_id = project_id
        if client is None:
            from google.cloud import secretmanager

            client = secretmanager.SecretManagerServiceClient()
        self._client = client

    async def access_latest(self, secret_name: str) -> str:
        name = f"projects/{self.project_id}/secrets/{secret_name}/versions/latest"

        def access() -> str:
            response = self._client.access_secret_version(request={"name": name})
            return response.payload.data.decode("utf-8")

        return await asyncio.to_thread(access)

    async def add_version(self, secret_name: str, value: str) -> None:
        parent = f"projects/{self.project_id}/secrets/{secret_name}"

        def add() -> None:
            self._client.add_secret_version(
                request={
                    "parent": parent,
                    "payload": {"data": value.encode("utf-8")},
                }
            )

        await asyncio.to_thread(add)


@dataclass(frozen=True)
class RefreshedCredentials:
    access_token: str
    refresh_token: str


class CommerceGovCredentialProvider:
    """Rotating OAuth credentials with an instance-local single-flight barrier.

    HACKATHON_REFRESH_SINGLE_INSTANCE_BOUNDARY: rotating refresh tokens require
    Cloud Run max instances = 1 until a cross-instance lock is implemented.
    """

    def __init__(
        self,
        *,
        access_token: str,
        token_store: TokenStore,
        access_secret: str,
        refresh_secret: str,
        token_url: str,
        client_id: str,
        http_client_factory: Callable[[], Any] = httpx.AsyncClient,
    ):
        self._access_token = access_token
        self._token_store = token_store
        self._access_secret = access_secret
        self._refresh_secret = refresh_secret
        self._token_url = token_url
        self._client_id = client_id
        self._http_client_factory = http_client_factory
        self._refresh_lock = asyncio.Lock()

    @property
    def access_token(self) -> str:
        return self._access_token

    async def refresh_after_auth_failure(self, failed_access_token: str) -> str:
        async with self._refresh_lock:
            if self._access_token != failed_access_token:
                return self._access_token

            try:
                refresh_token = await self._token_store.access_latest(self._refresh_secret)
            except Exception:
                raise OAuthRefreshError("oauth_refresh_unavailable") from None

            refreshed = await self._request_refresh(refresh_token)

            # Persist the replacement refresh token first. If the access-secret
            # write then fails, a cold start can recover without reusing the
            # rotated one-time refresh credential.
            try:
                await self._token_store.add_version(
                    self._refresh_secret, refreshed.refresh_token
                )
                await self._token_store.add_version(
                    self._access_secret, refreshed.access_token
                )
            except Exception:
                raise OAuthRefreshError("oauth_refresh_persistence_failed") from None

            self._access_token = refreshed.access_token
            return self._access_token

    async def _request_refresh(self, refresh_token: str) -> RefreshedCredentials:
        try:
            async with self._http_client_factory() as client:
                response = await client.post(
                    self._token_url,
                    data={
                        "grant_type": "refresh_token",
                        "client_id": self._client_id,
                        "refresh_token": refresh_token,
                    },
                    timeout=10.0,
                )
        except httpx.RequestError:
            raise OAuthRefreshError("oauth_refresh_unavailable") from None

        if response.status_code != 200:
            error = _response_error(response)
            if error == "invalid_grant":
                raise OAuthRefreshError("oauth_refresh_invalid_grant")
            raise OAuthRefreshError("oauth_refresh_unavailable")

        try:
            payload = response.json()
        except (TypeError, ValueError):
            raise OAuthRefreshError("oauth_refresh_unavailable") from None

        if not isinstance(payload, dict):
            raise OAuthRefreshError("oauth_refresh_unavailable")
        access_token = payload.get("access_token")
        replacement_refresh_token = payload.get("refresh_token")
        token_type = payload.get("token_type")
        expires_in = payload.get("expires_in")
        scopes = set(str(payload.get("scope", "")).split())
        if (
            not isinstance(access_token, str)
            or not access_token
            or not isinstance(replacement_refresh_token, str)
            or not replacement_refresh_token
            or token_type != "Bearer"
            or not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or expires_in <= 0
            or scopes != {REQUIRED_SCOPE}
        ):
            raise OAuthRefreshError("oauth_refresh_unavailable")

        return RefreshedCredentials(access_token, replacement_refresh_token)


def _response_error(response: Any) -> str | None:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return None
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return payload["error"]
    return None
