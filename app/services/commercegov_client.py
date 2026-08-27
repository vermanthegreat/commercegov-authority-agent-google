import httpx
from typing import Any
import logging

from app.models import CommerceGovProposalV1
from app.services.commercegov_credentials import (
    CommerceGovCredentialProvider,
    OAuthRefreshError,
)

logger = logging.getLogger("uvicorn.error")

class CommerceGovClientError(Exception):
    pass

class CommerceGovDeterministicError(CommerceGovClientError):
    pass

class CommerceGovTransientError(CommerceGovClientError):
    pass

class CommerceGovClient:
    def __init__(
        self,
        base_url: str | None,
        api_token: str | None,
        credential_provider: CommerceGovCredentialProvider | None = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_token = api_token
        self.credential_provider = credential_provider

    async def submit_proposal(self, proposal: CommerceGovProposalV1) -> str:
        """
        Submits a governed proposal and returns the resulting proposal_id.
        """
        access_token = (
            self.credential_provider.access_token
            if self.credential_provider is not None
            else self.api_token
        )
        if not self.base_url or not access_token:
            logger.warning("CommerceGov API not configured, skipping proposal submission")
            return "skipped-not-configured"

        # Support both bare IDs and Shopify GIDs for the URL path
        target_id_part = proposal.target_id.split("/")[-1]
        url = f"{self.base_url}/shops/{proposal.shop_id}/{proposal.target_type}s/{target_id_part}/proposals"
        payload = {
            "changes": proposal.requested_changes,
            "idempotency_key": proposal.idempotency_key,
        }

        async with httpx.AsyncClient() as client:
            resp = await self._post(client, url, access_token, payload)
            if self.credential_provider is not None and _is_refreshable_auth_failure(resp):
                try:
                    access_token = await self.credential_provider.refresh_after_auth_failure(
                        access_token
                    )
                except OAuthRefreshError as exc:
                    raise CommerceGovDeterministicError(exc.code) from exc
                resp = await self._post(client, url, access_token, payload)

        if resp.status_code == 201 or resp.status_code == 200:
            data = resp.json()
            return data.get("proposal_id", "unknown")
            
        if resp.status_code in (400, 401, 403, 404, 422):
            raise CommerceGovDeterministicError(f"commercegov_error_{resp.status_code}")

        raise CommerceGovTransientError(f"commercegov_error_{resp.status_code}")

    @staticmethod
    async def _post(client, url: str, access_token: str, payload: dict[str, Any]):
        try:
            return await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=10.0,
            )
        except httpx.RequestError:
            raise CommerceGovTransientError("commercegov_transport_error") from None


def _is_refreshable_auth_failure(response: Any) -> bool:
    if response.status_code != 401:
        return False
    errors: set[str] = set()
    try:
        payload = response.json()
    except (TypeError, ValueError):
        payload = None

    def collect(value: Any) -> None:
        if isinstance(value, str):
            errors.add(value)
        elif isinstance(value, dict):
            for nested in value.values():
                collect(nested)

    collect(payload)
    authenticate = response.headers.get("WWW-Authenticate", "")
    if 'error="invalid_token"' in authenticate:
        errors.add("invalid_token")
    return bool(errors & {"invalid_token", "token_expired"})
