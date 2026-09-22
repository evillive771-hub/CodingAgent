from pathlib import Path

import pytest

from aicoding_agent.adapters.playwright import (
    BrowserCapture,
    PlaywrightBrowserVerifier,
    PlaywrightSettings,
)
from aicoding_agent.adapters.ports import DeploymentReceipt
from aicoding_agent.domain.models import TaskContract


class FakeBrowserSession:
    def __init__(
        self,
        *,
        console_errors: list[str] | None = None,
        failed_requests: list[str] | None = None,
    ) -> None:
        self.console_errors = console_errors or []
        self.failed_requests = failed_requests or []
        self.urls: list[str] = []

    async def capture(self, url, settings, evidence_dir):
        del settings
        self.urls.append(url)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        screenshot = evidence_dir / "page.png"
        screenshot.write_bytes(b"png")
        return BrowserCapture(
            ok=not self.console_errors,
            url=url,
            status_code=200,
            console_errors=self.console_errors,
            failed_requests=self.failed_requests,
            evidence_refs=[str(screenshot)],
        )


def receipt(location: str = "https://test.internal/builds/deploy-1") -> DeploymentReceipt:
    return DeploymentReceipt(
        deployment_id="deploy-1",
        artifact_hash="a" * 64,
        location=location,
    )


def contract() -> TaskContract:
    return TaskContract(
        task_id="browser-test",
        objective="Verify the test deployment",
        acceptance_criteria=["Page is ready"],
    )


@pytest.mark.asyncio
async def test_playwright_verifier_collects_report_and_evidence(tmp_path: Path) -> None:
    session = FakeBrowserSession()
    verifier = PlaywrightBrowserVerifier(
        PlaywrightSettings(
            path="counter",
            ready_selector="#value",
            expected_text="0",
            allowed_hosts={"test.internal"},
        ),
        tmp_path,
        session=session,
    )

    report = await verifier.verify(receipt(), contract())

    assert report.ok is True
    assert session.urls == ["https://test.internal/builds/deploy-1/counter"]
    assert len(report.evidence_refs) == 2
    assert Path(report.evidence_refs[-1]).is_file()
    assert report.details["status_code"] == 200


@pytest.mark.asyncio
async def test_playwright_verifier_rejects_non_allowlisted_url_with_evidence(
    tmp_path: Path,
) -> None:
    verifier = PlaywrightBrowserVerifier(
        PlaywrightSettings(allowed_hosts={"test.internal"}),
        tmp_path,
        session=FakeBrowserSession(),
    )

    report = await verifier.verify(receipt("https://untrusted.invalid"), contract())

    assert report.ok is False
    assert "allowlist" in report.summary
    assert Path(report.evidence_refs[-1]).is_file()


@pytest.mark.asyncio
async def test_console_errors_fail_the_structured_report(tmp_path: Path) -> None:
    verifier = PlaywrightBrowserVerifier(
        PlaywrightSettings(allowed_hosts={"test.internal"}),
        tmp_path,
        session=FakeBrowserSession(console_errors=["ReferenceError: broken"]),
    )

    report = await verifier.verify(receipt(), contract())

    assert report.ok is False
    assert report.details["console_errors"] == ["ReferenceError: broken"]


@pytest.mark.asyncio
async def test_browser_evidence_redacts_secrets_and_url_queries(tmp_path: Path) -> None:
    verifier = PlaywrightBrowserVerifier(
        PlaywrightSettings(allowed_hosts={"test.internal"}),
        tmp_path,
        session=FakeBrowserSession(
            console_errors=["api_key=must-not-persist"],
            failed_requests=["https://api.internal/data?access_token=must-not-persist"],
        ),
    )

    report = await verifier.verify(receipt(), contract())
    persisted = Path(report.evidence_refs[-1]).read_text(encoding="utf-8")

    assert "must-not-persist" not in persisted
    assert report.details["console_errors"] == ["api_key=<redacted>"]
    assert report.details["failed_requests"] == ["https://api.internal/data"]
