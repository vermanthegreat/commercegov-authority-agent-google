import httpx
from typing import Any
import logging

logger = logging.getLogger("uvicorn.error")

class CommerceGovClientError(Exception):
    pass

class CommerceGovDeterministicError(CommerceGovClientError):
    pass

class CommerceGovTransientError(CommerceGovClientError):
    pass

class CommerceGovClient:
    def __init__(self, base_url: str | None, api_token: str | None):
        self.base_url = (base_url or "").rstrip("/")
        self.api_token = api_token

    async def submit_proposal(self, shop_id: str, product_id: str, changes: dict[str, Any], idempotency_key: str) -> str:
        """
        Submits a governed proposal and returns the resulting proposal_id.
        """
        if not self.base_url or not self.api_token:
            logger.warning("CommerceGov API not configured, skipping proposal submission")
            return "skipped-not-configured"

        url = f"{self.base_url}/shops/{shop_id}/products/{product_id}/proposals"
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }
        payload = {
            "changes": changes,
            "idempotency_key": idempotency_key
        }

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(url, headers=headers, json=payload, timeout=10.0)
            except httpx.RequestError as e:
                raise CommerceGovTransientError(f"Transport error: {e}") from e

        if resp.status_code == 201 or resp.status_code == 200:
            data = resp.json()
            return data.get("proposal_id", "unknown")
            
        if resp.status_code in (400, 401, 403, 404, 422):
            raise CommerceGovDeterministicError(f"Deterministic error {resp.status_code}: {resp.text}")

        raise CommerceGovTransientError(f"Ambiguous error {resp.status_code}: {resp.text}")