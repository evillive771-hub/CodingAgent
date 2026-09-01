import httpx
import pytest

from aicoding_agent.adapters.teambition import (
    TeambitionClient,
    TeambitionConfigurationError,
    TeambitionSettings,
)


@pytest.mark.asyncio
async def test_teambition_read_adapter_uses_required_headers_and_maps_ticket() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-token"
        assert request.headers["x-tenant-id"] == "tenant-1"
        assert request.headers["x-tenant-type"] == "organization"
        assert request.headers["x-operator-id"] == "operator-1"
        assert request.url.params["taskId"] == "task-1"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "result": [
                    {
                        "id": "task-1",
                        "content": "Frontend bug",
                        "note": "Fix the button",
                        "projectId": "project-1",
                        "executorId": "user-1",
                        "stageId": "doing",
                        "tfsId": "open",
                        "sfcId": "bug",
                        "customfields": [],
                    }
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = TeambitionClient(
        TeambitionSettings(
            access_token="test-token",
            tenant_id="tenant-1",
            operator_id="operator-1",
            project_id="project-1",
        ),
        client=client,
    )

    ticket = await adapter.fetch_ticket("task-1")

    assert ticket.title == "Frontend bug"
    assert ticket.status == "doing"
    assert ticket.metadata["task_flow_status_id"] == "open"
    with pytest.raises(TeambitionConfigurationError):
        await adapter.transition_to_testing(
            "task-1",
            revision="abc",
            evidence_refs=[],
            idempotency_key="run:tb",
        )
    await client.aclose()
