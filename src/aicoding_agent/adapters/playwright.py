from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aicoding_agent.adapters.ports import DeploymentReceipt, VerificationReport
from aicoding_agent.domain.models import TaskContract


class PlaywrightVerificationError(RuntimeError):
    pass


_SECRET_PATTERN = re.compile(
    r"(?i)(authorization|api[_-]?key|access[_-]?token|password)(\s*[:=]\s*)([^\s,;]+)"
)


def _redact(value: str) -> str:
    return _SECRET_PATTERN.sub(r"\1\2<redacted>", value)


def _safe_url(value: str) -> str:
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.hostname:
        return "<invalid-url>"
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}{parsed.path}"


class BrowserModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlaywrightSettings(BrowserModel):
    browser: Literal["chromium", "firefox", "webkit"] = "chromium"
    headless: bool = True
    path: str = ""
    ready_selector: str = "body"
    expected_text: str | None = None
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    allowed_hosts: set[str] = Field(min_length=1)
    record_video: bool = True
    fail_on_console_error: bool = True
    fail_on_request_failure: bool = True

    @field_validator("path")
    @classmethod
    def path_must_be_relative(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme or parsed.netloc or ".." in value.replace("\\", "/").split("/"):
            raise ValueError("Playwright path must be relative to the deployment URL")
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def hosts_are_normalized(cls, values: set[str]) -> set[str]:
        normalized = {value.strip().lower() for value in values}
        if any(not value or "/" in value or ":" in value for value in normalized):
            raise ValueError("allowed_hosts entries must contain hostnames only")
        return normalized


class BrowserCapture(BrowserModel):
    ok: bool
    url: str
    status_code: int | None = None
    error: str | None = None
    console_errors: list[str] = Field(default_factory=list)
    failed_requests: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class BrowserSession(Protocol):
    async def capture(
        self,
        url: str,
        settings: PlaywrightSettings,
        evidence_dir: Path,
    ) -> BrowserCapture: ...


class RealPlaywrightSession:
    async def capture(
        self,
        url: str,
        settings: PlaywrightSettings,
        evidence_dir: Path,
    ) -> BrowserCapture:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise PlaywrightVerificationError(
                "Install the 'delivery' extra and a Playwright browser"
            ) from exc

        evidence_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = evidence_dir / "page.png"
        trace_path = evidence_dir / "trace.zip"
        video_dir = evidence_dir / "video"
        console_errors: list[str] = []
        failed_requests: list[str] = []
        evidence_refs: list[str] = []
        status_code: int | None = None
        error: str | None = None
        timeout_ms = settings.timeout_seconds * 1000

        async with async_playwright() as playwright:
            browser_type = getattr(playwright, settings.browser)
            browser = await browser_type.launch(headless=settings.headless)
            context = await browser.new_context(
                record_video_dir=str(video_dir) if settings.record_video else None
            )
            await context.tracing.start(screenshots=True, snapshots=True, sources=True)
            page = await context.new_page()
            page.on(
                "console",
                lambda message: (
                    console_errors.append(_redact(message.text[:2000]))
                    if message.type == "error"
                    else None
                ),
            )
            page.on(
                "pageerror",
                lambda exception: console_errors.append(_redact(str(exception)[:2000])),
            )
            page.on(
                "requestfailed",
                lambda request: failed_requests.append(_safe_url(request.url[:2000])),
            )
            try:
                response = await page.goto(
                    url,
                    wait_until="networkidle",
                    timeout=timeout_ms,
                )
                status_code = response.status if response is not None else None
                if status_code is not None and status_code >= 400:
                    raise PlaywrightVerificationError(f"Navigation returned HTTP {status_code}")
                locator = page.locator(settings.ready_selector).first
                await locator.wait_for(state="visible", timeout=timeout_ms)
                if settings.expected_text is not None:
                    content = await locator.inner_text(timeout=timeout_ms)
                    if settings.expected_text not in content:
                        raise PlaywrightVerificationError(
                            f"Expected text was not found in {settings.ready_selector!r}"
                        )
            except Exception as exc:
                error = _redact(f"{type(exc).__name__}: {exc}"[:5000])
            finally:
                try:
                    await page.screenshot(path=str(screenshot_path), full_page=True)
                    evidence_refs.append(str(screenshot_path))
                except Exception as exc:
                    console_errors.append(
                        _redact(f"screenshot failed: {type(exc).__name__}: {exc}"[:2000])
                    )
                try:
                    await context.tracing.stop(path=str(trace_path))
                    evidence_refs.append(str(trace_path))
                except Exception as exc:
                    console_errors.append(
                        _redact(f"trace failed: {type(exc).__name__}: {exc}"[:2000])
                    )
                await context.close()
                await browser.close()

        if settings.record_video and video_dir.is_dir():
            evidence_refs.extend(
                str(path) for path in sorted(video_dir.glob("*")) if path.is_file()
            )
        ok = error is None
        if settings.fail_on_console_error and console_errors:
            ok = False
        if settings.fail_on_request_failure and failed_requests:
            ok = False
        return BrowserCapture(
            ok=ok,
            url=url,
            status_code=status_code,
            error=error,
            console_errors=console_errors[:100],
            failed_requests=failed_requests[:100],
            evidence_refs=evidence_refs,
        )


class PlaywrightBrowserVerifier:
    def __init__(
        self,
        settings: PlaywrightSettings,
        evidence_root: Path,
        *,
        session: BrowserSession | None = None,
    ) -> None:
        self.settings = settings
        self.evidence_root = evidence_root.resolve()
        self.session = session or RealPlaywrightSession()

    async def verify(
        self,
        receipt: DeploymentReceipt,
        contract: TaskContract,
    ) -> VerificationReport:
        del contract
        url = receipt.location.rstrip("/")
        if self.settings.path:
            url = f"{url}/{self.settings.path.lstrip('/')}"
        parsed = urlparse(url)
        evidence_dir = self.evidence_root / receipt.deployment_id
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.hostname not in self.settings.allowed_hosts
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            capture = BrowserCapture(
                ok=False,
                url="<rejected-url>",
                error="Deployment URL is outside the browser allowlist",
            )
        else:
            try:
                capture = await self.session.capture(url, self.settings, evidence_dir)
            except Exception as exc:
                capture = BrowserCapture(
                    ok=False,
                    url=url,
                    error=_redact(f"{type(exc).__name__}: {exc}"[:5000]),
                )
        report_path = evidence_dir / "verification-report.json"
        safe_error = _redact(capture.error) if capture.error is not None else None
        safe_console_errors = [_redact(value) for value in capture.console_errors]
        safe_failed_requests = [_safe_url(value) for value in capture.failed_requests]
        report = VerificationReport(
            ok=capture.ok,
            summary=(
                "Playwright verification passed"
                if capture.ok
                else f"Playwright verification failed: {safe_error or 'browser evidence failed'}"
            ),
            evidence_refs=[*capture.evidence_refs, str(report_path)],
            details={
                "url": capture.url,
                "status_code": capture.status_code,
                "console_errors": safe_console_errors,
                "failed_requests": safe_failed_requests,
            },
        )
        self._atomic_json(report_path, report.model_dump(mode="json"))
        return report

    @staticmethod
    def _atomic_json(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
