import pytest

from app.models import CommerceGovProposalV1
from app.services import commercegov_client as client_module
from app.services.commercegov_client import (
    CommerceGovClient,
    CommerceGovDeterministicError,
)


class _Response:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.text = f"status={status_code}"

    def json(self) -> dict[str, str]:
        return {"proposal_id": "proposal-123"}


class _AsyncClient:
    response_status = 201
    request: dict[str, object] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def post(self, url, **kwargs):
        type(self).request = {"url": url, **kwargs}
        return _Response(type(self).response_status)


@pytest.fixture
def proposal() -> CommerceGovProposalV1:
    return CommerceGovProposalV1(
        event_id="event-1",
        event_fingerprint="fingerprint-1",
        attempt=1,
        shop_id="controlled-demo.myshopify.com",
        target_type="product",
        target_id="gid://shopify/Product/7887756656717",
        requested_changes={"title": "Governed title"},
        authority_classification="READY_FOR_GOVERNED_EXECUTION",
        idempotency_key="idem-key-123",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [200, 201])
async def test_submit_proposal_uses_public_contract_only(
    monkeypatch: pytest.MonkeyPatch,
    proposal: CommerceGovProposalV1,
    status_code: int,
):
    _AsyncClient.response_status = status_code
    monkeypatch.setattr(client_module.httpx, "AsyncClient", _AsyncClient)
    client = CommerceGovClient(
        "https://app.commercegov.io/api/integration/v1/",
        "test-token-not-a-secret",
    )

    proposal_id = await client.submit_proposal(proposal)

    assert proposal_id == "proposal-123"
    assert _AsyncClient.request["url"] == (
        "https://app.commercegov.io/api/integration/v1/shops/"
        "controlled-demo.myshopify.com/products/7887756656717/proposals"
    )
    assert _AsyncClient.request["json"] == {
        "changes": {"title": "Governed title"},
        "idempotency_key": "idem-key-123",
    }
    headers = _AsyncClient.request["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer test-token-not-a-secret"
    assert _AsyncClient.request["timeout"] == 10.0
    assert not hasattr(client, "approve_proposal")
    assert not hasattr(client, "apply_proposal")


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422])
async def test_submit_proposal_keeps_deterministic_error_classification(
    monkeypatch: pytest.MonkeyPatch,
    proposal: CommerceGovProposalV1,
    status_code: int,
):
    _AsyncClient.response_status = status_code
    monkeypatch.setattr(client_module.httpx, "AsyncClient", _AsyncClient)
    client = CommerceGovClient(
        "https://app.commercegov.io/api/integration/v1",
        "test-token-not-a-secret",
    )

    with pytest.raises(CommerceGovDeterministicError, match=str(status_code)):
        await client.submit_proposal(proposal)
