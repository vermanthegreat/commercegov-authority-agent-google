import asyncio
from collections import deque
from typing import Any

import pytest

from app.models import CommerceGovProposalV1
from app.services import commercegov_client as client_module
from app.services.commercegov_client import (
    CommerceGovClient,
    CommerceGovDeterministicError,
    CommerceGovTransientError,
)
from app.services.commercegov_credentials import (
    CommerceGovCredentialProvider,
    OAuthRefreshError,
)


ACCESS_SECRET = "commercegov-taskmaster-api-token"
REFRESH_SECRET = "commercegov-taskmaster-refresh-token"
TOKEN_URL = "https://app.commercegov.io/oauth/integration/token"


class Response:
    def __init__(
        self,
        status_code: int,
        payload: Any,
        *,
        headers: dict[str, str] | None = None,
        text: str = "",
    ):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


class QueueClient:
    def __init__(self, responses: deque[Response], requests: list[dict[str, Any]]):
        self.responses = responses
        self.requests = requests

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def post(self, url, **kwargs):
        self.requests.append({"url": url, **kwargs})
        return self.responses.popleft()


class QueueFactory:
    def __init__(self, *responses: Response):
        self.responses = deque(responses)
        self.requests: list[dict[str, Any]] = []

    def __call__(self):
        return QueueClient(self.responses, self.requests)


class Store:
    def __init__(self, refresh_token: str = "old-refresh"):
        self.refresh_token = refresh_token
        self.accesses = 0
        self.writes: list[tuple[str, str]] = []
        self.fail_on: str | None = None

    async def access_latest(self, secret_name: str) -> str:
        assert secret_name == REFRESH_SECRET
        self.accesses += 1
        return self.refresh_token

    async def add_version(self, secret_name: str, value: str) -> None:
        if secret_name == self.fail_on:
            raise RuntimeError(f"write failed for {secret_name}")
        self.writes.append((secret_name, value))
        if secret_name == REFRESH_SECRET:
            self.refresh_token = value


def refreshed_response(
    *, scope: str = "proposals:write", access: str = "new-access", refresh: str = "new-refresh"
) -> Response:
    return Response(
        200,
        {
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": 3600,
            "scope": scope,
            "token_type": "Bearer",
        },
    )


def provider(store: Store, refresh_http: QueueFactory) -> CommerceGovCredentialProvider:
    return CommerceGovCredentialProvider(
        access_token="old-access",
        token_store=store,
        access_secret=ACCESS_SECRET,
        refresh_secret=REFRESH_SECRET,
        token_url=TOKEN_URL,
        client_id="taskmaster-hackathon-client-direct",
        http_client_factory=refresh_http,
    )


@pytest.fixture
def proposal() -> CommerceGovProposalV1:
    return CommerceGovProposalV1(
        event_id="event-oauth",
        event_fingerprint="fingerprint-oauth",
        attempt=1,
        shop_id="controlled-demo.myshopify.com",
        target_type="product",
        target_id="gid://shopify/Product/7887756656717",
        requested_changes={"title": "Governed title"},
        authority_classification="READY_FOR_GOVERNED_EXECUTION",
        idempotency_key="oauth-idem-key",
    )


@pytest.mark.asyncio
async def test_valid_access_token_does_not_refresh(monkeypatch, proposal):
    proposal_http = QueueFactory(Response(201, {"proposal_id": "409"}))
    refresh_http = QueueFactory(refreshed_response())
    store = Store()
    monkeypatch.setattr(client_module.httpx, "AsyncClient", proposal_http)
    client = CommerceGovClient(
        "https://app.commercegov.io/api/integration/v1",
        "old-access",
        provider(store, refresh_http),
    )

    assert await client.submit_proposal(proposal) == "409"
    assert store.accesses == 0
    assert refresh_http.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("oauth_error", ["token_expired", "invalid_token"])
async def test_canonical_401_refreshes_and_retries_once(
    monkeypatch, proposal, oauth_error
):
    proposal_http = QueueFactory(
        Response(401, {"error": oauth_error}),
        Response(201, {"proposal_id": "409"}),
    )
    refresh_http = QueueFactory(refreshed_response())
    store = Store()
    monkeypatch.setattr(client_module.httpx, "AsyncClient", proposal_http)
    client = CommerceGovClient(
        "https://app.commercegov.io/api/integration/v1",
        "old-access",
        provider(store, refresh_http),
    )

    assert await client.submit_proposal(proposal) == "409"
    assert len(proposal_http.requests) == 2
    assert proposal_http.requests[0]["headers"]["Authorization"] == "Bearer old-access"
    assert proposal_http.requests[1]["headers"]["Authorization"] == "Bearer new-access"
    assert refresh_http.requests[0]["url"] == TOKEN_URL
    assert refresh_http.requests[0]["data"] == {
        "grant_type": "refresh_token",
        "client_id": "taskmaster-hackathon-client-direct",
        "refresh_token": "old-refresh",
    }
    assert store.writes == [
        (REFRESH_SECRET, "new-refresh"),
        (ACCESS_SECRET, "new-access"),
    ]


@pytest.mark.asyncio
async def test_failed_retry_does_not_start_second_refresh(monkeypatch, proposal):
    proposal_http = QueueFactory(
        Response(401, {"error": "token_expired"}),
        Response(401, {"error": "token_expired"}),
    )
    refresh_http = QueueFactory(refreshed_response())
    store = Store()
    monkeypatch.setattr(client_module.httpx, "AsyncClient", proposal_http)
    client = CommerceGovClient(
        "https://app.commercegov.io/api/integration/v1",
        "old-access",
        provider(store, refresh_http),
    )

    with pytest.raises(CommerceGovDeterministicError, match="commercegov_error_401"):
        await client.submit_proposal(proposal)
    assert len(proposal_http.requests) == 2
    assert len(refresh_http.requests) == 1


@pytest.mark.asyncio
async def test_invalid_grant_fails_closed(monkeypatch, proposal):
    proposal_http = QueueFactory(Response(401, {"error": "invalid_token"}))
    refresh_http = QueueFactory(Response(400, {"error": "invalid_grant"}))
    store = Store()
    monkeypatch.setattr(client_module.httpx, "AsyncClient", proposal_http)
    client = CommerceGovClient(
        "https://app.commercegov.io/api/integration/v1",
        "old-access",
        provider(store, refresh_http),
    )

    with pytest.raises(CommerceGovDeterministicError) as exc_info:
        await client.submit_proposal(proposal)
    assert str(exc_info.value) == "oauth_refresh_invalid_grant"
    assert len(proposal_http.requests) == 1
    assert store.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_secret", [REFRESH_SECRET, ACCESS_SECRET])
async def test_persistence_failure_prevents_retry(
    monkeypatch, proposal, failed_secret
):
    proposal_http = QueueFactory(Response(401, {"error": "token_expired"}))
    refresh_http = QueueFactory(refreshed_response())
    store = Store()
    store.fail_on = failed_secret
    monkeypatch.setattr(client_module.httpx, "AsyncClient", proposal_http)
    client = CommerceGovClient(
        "https://app.commercegov.io/api/integration/v1",
        "old-access",
        provider(store, refresh_http),
    )

    with pytest.raises(CommerceGovDeterministicError) as exc_info:
        await client.submit_proposal(proposal)
    assert str(exc_info.value) == "oauth_refresh_persistence_failed"
    assert len(proposal_http.requests) == 1
    expected_writes = (
        []
        if failed_secret == REFRESH_SECRET
        else [(REFRESH_SECRET, "new-refresh")]
    )
    assert store.writes == expected_writes


@pytest.mark.asyncio
async def test_scope_expansion_is_rejected_without_persistence():
    store = Store()
    credential_provider = provider(
        store, QueueFactory(refreshed_response(scope="proposals:write proposals:approve"))
    )

    with pytest.raises(OAuthRefreshError) as exc_info:
        await credential_provider.refresh_after_auth_failure("old-access")
    assert str(exc_info.value) == "oauth_refresh_unavailable"
    assert store.writes == []


@pytest.mark.asyncio
async def test_refresh_requires_positive_expiry():
    response = refreshed_response()
    response._payload["expires_in"] = 0
    store = Store()
    credential_provider = provider(store, QueueFactory(response))

    with pytest.raises(OAuthRefreshError, match="oauth_refresh_unavailable"):
        await credential_provider.refresh_after_auth_failure("old-access")
    assert store.writes == []


@pytest.mark.asyncio
async def test_concurrent_refresh_is_single_flight():
    store = Store()
    refresh_http = QueueFactory(refreshed_response())
    credential_provider = provider(store, refresh_http)

    results = await asyncio.gather(
        credential_provider.refresh_after_auth_failure("old-access"),
        credential_provider.refresh_after_auth_failure("old-access"),
    )

    assert results == ["new-access", "new-access"]
    assert store.accesses == 1
    assert len(refresh_http.requests) == 1
    assert len(store.writes) == 2


@pytest.mark.asyncio
async def test_errors_never_echo_credentials(monkeypatch, proposal):
    access = "access-value-must-not-leak"
    refresh = "refresh-value-must-not-leak"
    proposal_http = QueueFactory(
        Response(500, {"error": "server_error"}, text=f"{access} {refresh}")
    )
    monkeypatch.setattr(client_module.httpx, "AsyncClient", proposal_http)
    client = CommerceGovClient(
        "https://app.commercegov.io/api/integration/v1", access
    )

    with pytest.raises(CommerceGovTransientError) as exc_info:
        await client.submit_proposal(proposal)
    rendered = str(exc_info.value)
    assert access not in rendered
    assert refresh not in rendered


def test_propose_only_surface_has_no_execution_capabilities():
    assert not hasattr(CommerceGovClient, "approve_proposal")
    assert not hasattr(CommerceGovClient, "apply_proposal")
    assert not hasattr(CommerceGovClient, "write_shopify")
