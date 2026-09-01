from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from aicoding_agent.adapters.ports import TicketSnapshot


class TeambitionError(RuntimeError):
    pass


class TeambitionConfigurationError(TeambitionError):
    pass


class TeambitionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_token: SecretStr
    tenant_id: str = Field(min_length=1)
    operator_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    base_url: str = "https://open.teambition.com"
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("base_url must be HTTPS and must not contain user information")
        return normalized


class TeambitionClient:
    """Read-capable Teambition Open Platform adapter.

    Transition-to-testing is intentionally blocked until the organization supplies its task-flow
    field mapping. Teambition projects may represent testing with different stageId/tfsId values.
    """

    def __init__(
        self,
        settings: TeambitionSettings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(settings.timeout_seconds))

    async def fetch_ticket(self, ticket_id: str) -> TicketSnapshot:
        body = await self._get("/api/v3/task/query", params={"taskId": ticket_id})
        results = body.get("result")
        if not isinstance(results, list) or not results:
            raise TeambitionError(f"Teambition task was not found: {ticket_id}")
        return self._ticket_snapshot(results[0])

    async def query_project_tasks(
        self,
        tql: str,
        *,
        page_size: int = 50,
        page_token: str | None = None,
    ) -> tuple[list[TicketSnapshot], str | None]:
        params: dict[str, str | int] = {"q": tql, "pageSize": page_size}
        if page_token:
            params["pageToken"] = page_token
        body = await self._get(
            f"/api/v3/project/{self.settings.project_id}/task/query",
            params=params,
        )
        results = body.get("result") or []
        if not isinstance(results, list):
            raise TeambitionError("Teambition task query returned an invalid result")
        return [self._ticket_snapshot(item) for item in results], body.get("nextPageToken")

    async def transition_to_testing(
        self,
        ticket_id: str,
        *,
        revision: str,
        evidence_refs: list[str],
        idempotency_key: str,
    ) -> None:
        raise TeambitionConfigurationError(
            "Teambition transition is blocked until testing stage/status IDs and the approved "
            "write-back fields are configured for the target project"
        )

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, str | int],
    ) -> dict[str, Any]:
        try:
            response = await self._client.get(
                f"{self.settings.base_url}{path}",
                headers=self._headers(),
                params=params,
            )
        except httpx.HTTPError as exc:
            raise TeambitionError(f"Teambition request failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise TeambitionError(f"Teambition returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise TeambitionError("Teambition returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise TeambitionError("Teambition response root must be an object")
        code = body.get("code")
        if isinstance(code, int) and code not in {0, 200}:
            request_id = body.get("requestId") or "unknown"
            raise TeambitionError(f"Teambition API error {code}; requestId={request_id}")
        return body

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.access_token.get_secret_value()}",
            "X-Tenant-Id": self.settings.tenant_id,
            "X-Tenant-Type": "organization",
            "X-Operator-Id": self.settings.operator_id,
        }

    @staticmethod
    def _ticket_snapshot(item: Any) -> TicketSnapshot:
        if not isinstance(item, dict) or not item.get("id") or not item.get("content"):
            raise TeambitionError("Teambition task payload is missing id or content")
        custom_fields = item.get("customfields") or []
        return TicketSnapshot(
            ticket_id=str(item["id"]),
            title=str(item["content"]),
            description=str(item.get("note") or ""),
            status=str(item.get("stageId") or item.get("tfsId") or "unknown"),
            assignee=str(item["executorId"]) if item.get("executorId") else None,
            metadata={
                "project_id": str(item.get("projectId") or ""),
                "stage_id": str(item.get("stageId") or ""),
                "task_flow_status_id": str(item.get("tfsId") or ""),
                "scenario_field_config_id": str(item.get("sfcId") or ""),
                "custom_field_count": str(len(custom_fields)),
            },
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
