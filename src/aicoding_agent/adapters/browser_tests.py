"""Host-owned, bounded Playwright Test execution for the coding loop.

Suites are trusted host configuration, never model supplied commands.  The Node
preload guards browser contexts; it is not an operating-system network sandbox.
"""

from __future__ import annotations

import asyncio
import ctypes
import fnmatch
import hashlib
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BrowserTestError(RuntimeError):
    """Invalid host configuration or untrustworthy execution evidence."""


class BrowserSuite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
    command: list[str] = Field(min_length=1)
    cwd: Path
    test_files: list[Path] = Field(min_length=1)
    expected_tests: list[str] = Field(min_length=1)
    preview_command: list[str] = Field(default_factory=list)
    preview_url: str | None = None
    preview_ready_seconds: float = Field(default=15, gt=0, le=120)
    allowed_origins: list[str] = Field(default_factory=list)
    input_globs: list[str] = Field(default_factory=lambda: ["**/*"])
    exclude_globs: list[str] = Field(
        default_factory=lambda: [
            ".git/**", "node_modules/**", ".venv/**", "__pycache__/**",
            ".aicoding-agent/**", ".env", ".env.*", "env.txt",
            "test-results/**", "playwright-report/**", ".next/**",
        ]
    )

    @field_validator("command", "preview_command")
    @classmethod
    def valid_command(cls, values: list[str]) -> list[str]:
        if any(not value or "\x00" in value for value in values):
            raise ValueError("commands must contain nonempty argv entries")
        return values

    @field_validator("expected_tests")
    @classmethod
    def unique_tests(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values) or len(values) != len(set(values)):
            raise ValueError("expected test titles must be nonempty and unique")
        return values

    @model_validator(mode="after")
    def local_targets(self) -> BrowserSuite:
        targets = [*self.allowed_origins]
        if self.preview_url:
            targets.append(self.preview_url)
        if not targets:
            raise ValueError("a local preview URL or allowed origin is required")
        for value in targets:
            parsed = urlparse(value)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username or parsed.password or parsed.fragment
            ):
                raise ValueError("browser targets must be credential-free loopback HTTP URLs")
        if self.preview_command and not self.preview_url:
            raise ValueError("preview_command requires preview_url")
        return self


class BrowserFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    test: str
    message: str
    expected: Any = None
    actual: Any = None
    step: str | None = None


class BrowserTestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verification_id: str
    attempt_id: str
    run_id: str
    suite_ref: str
    status: Literal["passed", "failed", "infra_error", "timeout"]
    input_fingerprint: str = ""
    suite_fingerprint: str = ""
    report_hash: str = ""
    guard_proof_hash: str = ""
    duration_seconds: float = 0
    exit_code: int | None = None
    executed: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    failures: list[BrowserFailure] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    reason: str | None = None


_SECRET = re.compile(
    r"(?i)(authorization|api[_-]?key|access[_-]?token|password)(\s*[:=]\s*)([^\s,;]+)"
)


def _redact(value: str, limit: int = 4000) -> str:
    return _SECRET.sub(r"\1\2<redacted>", value)[:limit]


def _bounded_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact(value)
    return _redact(json.dumps(value, ensure_ascii=False))


def _sha256(path: Path, limit: int = 8_000_000) -> str:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > limit:
        raise BrowserTestError(f"Missing, oversized, or symlink evidence: {path.name}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _environment() -> dict[str, str]:
    # Deliberately do not inherit NODE_OPTIONS, arbitrary secrets or model credentials.
    allowed = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
        "HOME", "USERPROFILE", "LOCALAPPDATA", "PLAYWRIGHT_BROWSERS_PATH",
        "LD_LIBRARY_PATH", "DISPLAY",
    }
    values = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    values.update(CI="1", NO_COLOR="1")
    return values


def _windows_job(pid: int) -> int:
    """Keep descendants owned even when their immediate parent exits first."""
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t),
            ("maximum", ctypes.c_size_t), ("active", wintypes.DWORD),
            ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits), ("io", ctypes.c_ulonglong * 6),
            ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
            ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    process = kernel.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not job or not process or not kernel.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ) or not kernel.AssignProcessToJobObject(job, process):
            raise BrowserTestError("Could not establish owned Windows process-tree cleanup")
        return job
    except BaseException:
        if job:
            kernel.CloseHandle(job)
        raise
    finally:
        if process:
            kernel.CloseHandle(process)


def _close_windows_job(job: int) -> None:
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle(job)


def parse_playwright_report(
    payload: dict[str, Any], expected_tests: list[str], exit_code: int,
    *, max_failures: int = 10,
) -> dict[str, Any]:
    """Accept only the complete fixed inventory, with no retries/flaky/skipped tests."""
    entries: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def walk(suites: list[dict[str, Any]]) -> None:
        for suite in suites:
            for spec in suite.get("specs", []):
                for test in spec.get("tests", []):
                    entries.append((spec["title"], spec, test))
            walk(suite.get("suites", []))

    try:
        walk(payload["suites"])
        names = [entry[0] for entry in entries]
        if sorted(names) != sorted(expected_tests) or not entries:
            raise BrowserTestError("Report does not match the complete frozen test inventory")
        if payload.get("errors"):
            raise BrowserTestError("Playwright reported collection or global setup errors")
        counts = {"executed": 0, "passed": 0, "failed": 0, "skipped": 0}
        failures: list[BrowserFailure] = []
        for name, spec, test in entries:
            results = test.get("results", [])
            if spec.get("only") or test.get("expectedStatus", "passed") != "passed":
                raise BrowserTestError("Focused or expected-failure tests are not valid evidence")
            if len(results) != 1:
                raise BrowserTestError("Missing test result or test retries are not allowed")
            result = results[0]
            status = result.get("status")
            if status == "skipped":
                counts["skipped"] += 1
                continue
            if status not in {"passed", "failed", "timedOut", "interrupted"}:
                raise BrowserTestError("Unknown test result status")
            counts["executed"] += 1
            if status == "passed" and test.get("status", "expected") == "expected":
                counts["passed"] += 1
            else:
                counts["failed"] += 1
                error = result.get("error") or next(iter(result.get("errors", [])), {})
                if not isinstance(error, dict):
                    raise BrowserTestError("Invalid test error")
                if len(failures) < max_failures:
                    failures.append(BrowserFailure(
                        test=name, message=_redact(str(error.get("message", status))),
                        expected=_bounded_value(error.get("expected")),
                        actual=_bounded_value(error.get("actual")),
                    ))
        if counts["skipped"]:
            raise BrowserTestError("Required tests were skipped")
        passed = counts["passed"] == len(expected_tests)
        if (exit_code == 0) != passed:
            raise BrowserTestError("Process exit code disagrees with test report")
        return {"status": "passed" if passed else "failed", **counts, "failures": failures}
    except (KeyError, TypeError, AttributeError) as exc:
        raise BrowserTestError("Malformed Playwright JSON report") from exc


# Runs in Playwright's Node workers, before the test runner imports playwright-core.
# Browser-context routes also cover redirects/subresources; service workers and
# external WebSockets are disabled. Host test code and OS processes remain trusted.
_NETWORK_GUARD = r"""
const fs = require('node:fs');
const core = require(require.resolve('playwright-core', {paths: [process.env.BROWSER_SUITE_CWD]}));
const origins = new Set(JSON.parse(process.env.BROWSER_ALLOWED_ORIGINS));
async function guard(context) {
  await context.route('**/*', async route => {
    const url = route.request().url();
    if (url.startsWith('data:') || url.startsWith('blob:')) return route.continue();
    let origin; try { origin = new URL(url).origin; } catch { return route.abort(); }
    if (!origins.has(origin)) return route.abort('blockedbyclient');
    // Playwright routing does not guarantee another callback on HTTP redirects.
    // Refuse redirects before they can escape the guarded local origin.
    const response = await route.fetch({maxRedirects: 0});
    if (response.status() >= 300 && response.status() < 400)
      return route.abort('blockedbyclient');
    return route.fulfill({response});
  });
  if (typeof context.routeWebSocket !== 'function') throw new Error('WebSocket guard unavailable');
  await context.routeWebSocket('**/*', socket => {
    let url; try { url = new URL(socket.url()); } catch { return socket.close(); }
    url.protocol = url.protocol === 'wss:' ? 'https:' : 'http:';
    if (origins.has(url.origin)) socket.connectToServer(); else socket.close();
  });
  fs.appendFileSync(process.env.BROWSER_GUARD_PROOF, 'guarded\n');
  return context;
}
for (const name of ['chromium', 'firefox', 'webkit']) {
  const type = core[name], launch = type.launch.bind(type);
  type.launch = async options => {
    const browser = await launch(options);
    const newContext = browser.newContext.bind(browser);
    browser.newContext = async options => guard(
      await newContext({...options, serviceWorkers:'block'}));
    return browser;
  };
  type.connect = type.connectOverCDP = async () => { throw new Error('Remote browsers disabled'); };
  type.launchPersistentContext = async () => { throw new Error('Persistent contexts disabled'); };
}
"""


class BrowserTestRunner:
    def __init__(
        self, suites: list[BrowserSuite], evidence_root: Path, *, max_report_bytes: int = 8_000_000,
        max_evidence_bytes: int = 64_000_000,
        max_input_bytes: int = 512_000_000,
    ) -> None:
        if len({suite.id for suite in suites}) != len(suites):
            raise BrowserTestError("Duplicate suite IDs")
        self.suites = {suite.id: suite for suite in suites}
        self.evidence_root = evidence_root.resolve()
        self.max_report_bytes = max_report_bytes
        self.max_evidence_bytes = max_evidence_bytes
        self.max_input_bytes = max_input_bytes
        self._frozen: dict[tuple[str, str], str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._jobs: dict[int, int] = {}

    @classmethod
    async def from_config(cls, path: Path, evidence_root: Path) -> BrowserTestRunner:
        config_path = path.resolve()
        payload = await asyncio.to_thread(config_path.read_text, encoding="utf-8")
        config = json.loads(payload)
        if not isinstance(config, dict) or set(config) != {"suites"}:
            raise BrowserTestError("Suite config must contain only a suites list")
        suites = []
        for entry in config["suites"]:
            values = dict(entry)
            values["cwd"] = (config_path.parent / values["cwd"]).resolve()
            values["test_files"] = [
                (values["cwd"] / file).resolve() for file in values["test_files"]
            ]
            # The config itself is an immutable execution input.
            if config_path not in values["test_files"]:
                values["test_files"].append(config_path)
            suites.append(BrowserSuite.model_validate(values))
        return cls(suites, evidence_root)

    def has_suite(self, suite_ref: str) -> bool:
        return suite_ref in self.suites

    def frozen_suite_fingerprints(self, workspace: Path) -> dict[str, str]:
        root = str(workspace.resolve())
        return {suite_ref: value for (directory, suite_ref), value in self._frozen.items()
                if directory == root}

    async def run_count(self, run_id: str, verification_id: str) -> int:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", run_id):
            raise BrowserTestError("Invalid run ID")

        def count() -> int:
            total = 0
            for path in (self.evidence_root / run_id).glob("*/attempt.json"):
                try:
                    if json.loads(path.read_text(encoding="utf-8")).get("verification_id") == (
                        verification_id
                    ):
                        total += 1
                except (OSError, ValueError):
                    # Corrupt attempts must not replenish a task's budget.
                    total += 1
            return total

        return await asyncio.to_thread(count)

    async def environment_failure_count(self, run_id: str, verification_id: str) -> int:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", run_id):
            raise BrowserTestError("Invalid run ID")

        def count() -> int:
            total = 0
            for path in (self.evidence_root / run_id).glob("*/attempt.json"):
                try:
                    attempt = json.loads(path.read_text(encoding="utf-8"))
                    if attempt.get("verification_id") != verification_id:
                        continue
                    receipt = json.loads(path.with_name("result.json").read_text(encoding="utf-8"))
                    if receipt.get("status") in {"infra_error", "timeout"}:
                        total += 1
                except (OSError, ValueError):
                    total += 1
            return total

        return await asyncio.to_thread(count)

    def _suite_fingerprint(self, suite: BrowserSuite, workspace: Path) -> str:
        if not suite.cwd.is_dir() or _within(suite.cwd.resolve(), workspace):
            raise BrowserTestError("Test suite cwd must be a host directory outside the workspace")
        digest = hashlib.sha256(suite.model_dump_json().encode())
        for file in sorted(suite.test_files, key=str):
            path = file if file.is_absolute() else suite.cwd / file
            if _within(path.resolve(), workspace) or path.is_symlink():
                raise BrowserTestError("Fixed tests must be outside the editable workspace")
            content = path.read_bytes()
            if re.search(rb"\b(?:test|describe|it)(?:\.describe)?\.only\s*\(", content):
                raise BrowserTestError("Focused tests are prohibited")
            digest.update(str(path.resolve()).encode())
            digest.update(content)
        return digest.hexdigest()

    def _input_fingerprint(self, workspace: Path, suite: BrowserSuite) -> str:
        digest = hashlib.sha256()
        if suite.input_globs == ["**/*"]:
            files = []
            for directory, directories, names in os.walk(workspace, followlinks=False):
                base = Path(directory)
                retained = []
                for name in directories:
                    child = base / name
                    relative = child.relative_to(workspace).as_posix()
                    if any(fnmatch.fnmatch(f"{relative}/_probe", glob)
                           for glob in suite.exclude_globs):
                        continue
                    if child.is_symlink() or child.is_junction():
                        raise BrowserTestError("Verification inputs cannot contain symbolic links")
                    retained.append(name)
                directories[:] = retained
                files.extend(base / name for name in names)
            files.sort()
        else:
            files = sorted({file for glob in suite.input_globs for file in workspace.glob(glob)})
        total_bytes = 0
        for file in files:
            relative = file.relative_to(workspace).as_posix()
            if any(fnmatch.fnmatch(relative, glob) for glob in suite.exclude_globs):
                continue
            if file.is_symlink():
                raise BrowserTestError("Verification inputs cannot contain symbolic links")
            if not file.is_file():
                continue
            if not _within(file.resolve(), workspace):
                raise BrowserTestError("Verification input escapes the workspace")
            digest.update(relative.encode())
            # Large assets are valid test inputs, but never load one wholly into memory.
            with file.open("rb") as stream:
                while chunk := stream.read(1_048_576):
                    total_bytes += len(chunk)
                    if total_bytes > self.max_input_bytes:
                        raise BrowserTestError(
                            "Verification input exceeded the hashing size budget"
                        )
                    digest.update(chunk)
        return digest.hexdigest()

    async def freeze(self, workspace: Path) -> None:
        root = workspace.resolve()
        if not root.is_dir():
            raise BrowserTestError("Browser workspace does not exist")
        if _within(self.evidence_root, root):
            raise BrowserTestError("Browser evidence must be outside the editable workspace")
        for suite in self.suites.values():
            fingerprint = await asyncio.to_thread(self._suite_fingerprint, suite, root)
            key = (str(root), suite.id)
            old = self._frozen.setdefault(key, fingerprint)
            if old != fingerprint:
                raise BrowserTestError("Frozen suite has changed")

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if os.name == "nt":
            job = self._jobs.pop(process.pid, None)
            if job is not None:
                _close_windows_job(job)
            elif process.returncode is None:
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await process.wait()

    async def _spawn(self, argv: list[str], cwd: Path, environment: dict[str, str]):
        options: dict[str, Any] = {}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd), env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **options,
        )
        if os.name == "nt":
            try:
                self._jobs[process.pid] = _windows_job(process.pid)
            except BaseException:
                process.kill()
                await process.wait()
                raise
        return process

    @staticmethod
    async def _drain(process: asyncio.subprocess.Process, limit: int = 100_000) -> str:
        chunks: list[bytes] = []
        size = 0
        assert process.stdout is not None
        while chunk := await process.stdout.read(16_384):
            if size < limit:
                chunks.append(chunk[:limit - size])
                size += len(chunk)
        return _redact(b"".join(chunks).decode(errors="replace"), limit)

    async def _wait_bounded(
        self, process: asyncio.subprocess.Process, directory: Path,
    ) -> int:
        async def monitor() -> None:
            while True:
                size = await asyncio.to_thread(
                    lambda: sum(file.stat().st_size for file in directory.rglob("*")
                                if file.is_file())
                )
                if size > self.max_evidence_bytes:
                    raise BrowserTestError("Browser evidence exceeded size budget")
                await asyncio.sleep(0.1)

        waiter = asyncio.create_task(process.wait())
        watcher = asyncio.create_task(monitor())
        try:
            done, _ = await asyncio.wait([waiter, watcher], return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                await watcher
            return await waiter
        finally:
            waiter.cancel()
            watcher.cancel()
            await asyncio.gather(waiter, watcher, return_exceptions=True)

    async def _ready(self, url: str, wait_seconds: float, *, must_be_absent: bool = False) -> None:
        deadline = time.monotonic() + wait_seconds
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            while True:
                try:
                    response = await client.get(url, timeout=min(wait_seconds, 1))
                    if must_be_absent:
                        raise BrowserTestError(
                            "Preview address is already served by another process"
                        )
                    if 200 <= response.status_code < 300:
                        return
                    if 300 <= response.status_code < 400:
                        raise BrowserTestError("Preview readiness redirects are not allowed")
                except httpx.HTTPError:
                    if must_be_absent:
                        return
                if time.monotonic() >= deadline:
                    raise BrowserTestError("Preview did not become ready")
                await asyncio.sleep(0.05)

    async def run(
        self, *, workspace: Path, verification: Any, run_id: str, timeout_seconds: float,
    ) -> BrowserTestResult:
        root = workspace.resolve()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", run_id):
            raise BrowserTestError("Invalid run ID")
        if verification.suite_ref not in self.suites:
            raise BrowserTestError("Unknown browser suite")
        suite = self.suites[verification.suite_ref]
        result = BrowserTestResult(
            verification_id=verification.id, attempt_id=uuid4().hex,
            run_id=run_id, suite_ref=suite.id, status="infra_error",
        )
        async with self._locks.setdefault(str(root), asyncio.Lock()):
            await self.freeze(root)
            started = time.monotonic()
            directory = self.evidence_root / run_id / result.attempt_id
            await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=False)
            report = directory / "report.json"
            guard = directory / "network-guard.cjs"
            proof = directory / "guard-proof.txt"
            await asyncio.to_thread(
                (directory / "attempt.json").write_text,
                json.dumps({"verification_id": verification.id, "attempt_id": result.attempt_id}),
                encoding="utf-8",
            )
            result.evidence_refs = [str(report), str(directory / "execution.log")]
            result.suite_fingerprint = self._frozen[(str(root), suite.id)]
            result.input_fingerprint = await asyncio.to_thread(self._input_fingerprint, root, suite)
            environment = _environment()
            targets = suite.allowed_origins or [suite.preview_url]
            origins = [f"{urlparse(url).scheme}://{urlparse(url).netloc}" for url in targets if url]
            environment.update(
                EVAL_PROJECT_ROOT=str(root), BROWSER_WORKSPACE=str(root),
                PLAYWRIGHT_JSON_OUTPUT_NAME=str(report), BROWSER_SUITE_CWD=str(suite.cwd),
                BROWSER_TEST_BASE_URL=suite.preview_url or origins[0],
                PLAYWRIGHT_OUTPUT_DIR=str(directory / "test-results"),
                BROWSER_ALLOWED_ORIGINS=json.dumps(origins), BROWSER_GUARD_PROOF=str(proof),
                NODE_OPTIONS=f'--require "{guard.as_posix()}"',
            )
            await asyncio.to_thread(guard.write_text, _NETWORK_GUARD, encoding="utf-8")
            processes: list[asyncio.subprocess.Process] = []
            drains: list[asyncio.Task[str]] = []
            try:
                async with asyncio.timeout(timeout_seconds):
                    if suite.preview_command:
                        assert suite.preview_url is not None
                        await self._ready(suite.preview_url, 1, must_be_absent=True)
                        preview = await self._spawn(suite.preview_command, root, _environment())
                        processes.append(preview)
                        drains.append(asyncio.create_task(self._drain(preview)))
                        await self._ready(suite.preview_url, suite.preview_ready_seconds)
                        if preview.returncode is not None:
                            raise BrowserTestError("Owned preview exited before test execution")
                    process = await self._spawn(suite.command, suite.cwd, environment)
                    processes.append(process)
                    drains.append(asyncio.create_task(self._drain(process)))
                    result.exit_code = await self._wait_bounded(process, directory)
                    result.report_hash = await asyncio.to_thread(
                        _sha256, report, self.max_report_bytes,
                    )
                    content = await asyncio.to_thread(report.read_text, encoding="utf-8")
                    payload = json.loads(content)
                    parsed = parse_playwright_report(
                        payload, suite.expected_tests, result.exit_code,
                        max_failures=verification.max_failures_returned,
                    )
                    if not proof.is_file() or proof.stat().st_size == 0:
                        raise BrowserTestError("No guarded browser context was executed")
                    result.guard_proof_hash = await asyncio.to_thread(_sha256, proof, 8192)
                    current = await asyncio.to_thread(self._input_fingerprint, root, suite)
                    if current != result.input_fingerprint:
                        raise BrowserTestError("Workspace changed during browser verification")
                    frozen = await asyncio.to_thread(self._suite_fingerprint, suite, root)
                    if frozen != result.suite_fingerprint:
                        raise BrowserTestError("Fixed test suite changed during verification")
                    for key, value in parsed.items():
                        setattr(result, key, value)
            except TimeoutError:
                result.status, result.reason = "timeout", "Browser verification timed out"
            except asyncio.CancelledError:
                result.status, result.reason = "infra_error", "Browser verification interrupted"
                raise
            except (OSError, ValueError, BrowserTestError) as exc:
                result.status, result.reason = "infra_error", _redact(str(exc))
            finally:
                for process in reversed(processes):
                    await asyncio.shield(self._terminate(process))
                logs = await asyncio.gather(*drains, return_exceptions=True)
                await asyncio.to_thread(
                    (directory / "execution.log").write_text,
                    "\n".join(log for log in logs if isinstance(log, str)), encoding="utf-8",
                )
                result.duration_seconds = time.monotonic() - started
                await asyncio.to_thread(
                    (directory / "result.json").write_text,
                    result.model_dump_json(), encoding="utf-8",
                )
            size = await asyncio.to_thread(
                lambda: sum(file.stat().st_size for file in directory.rglob("*") if file.is_file())
            )
            if size > self.max_evidence_bytes:
                result.status = "infra_error"
                result.reason = "Browser evidence exceeded size budget"
            receipt = directory / "result.json"
            result.evidence_refs.append(str(receipt))
            await asyncio.to_thread(receipt.write_text, result.model_dump_json(), encoding="utf-8")
            return result

    async def revalidate(
        self, workspace: Path, result: BrowserTestResult | dict[str, Any], *,
        allow_failed: bool = False,
    ) -> bool:
        try:
            if isinstance(result, dict):
                result = BrowserTestResult.model_validate(result)
            suite = self.suites[result.suite_ref]
            root = workspace.resolve()
            if result.status not in ({"passed", "failed"} if allow_failed else {"passed"}):
                return False
            directory = self.evidence_root / result.run_id / result.attempt_id
            if not _within(directory.resolve(), self.evidence_root):
                return False
            if result.suite_fingerprint != self._frozen.get((str(root), suite.id)):
                return False
            suite_hash = await asyncio.to_thread(self._suite_fingerprint, suite, root)
            if suite_hash != result.suite_fingerprint:
                return False
            input_hash = await asyncio.to_thread(self._input_fingerprint, root, suite)
            if input_hash != result.input_fingerprint:
                return False
            report = directory / "report.json"
            report_hash = await asyncio.to_thread(_sha256, report, self.max_report_bytes)
            if report_hash != result.report_hash:
                return False
            guard_hash = await asyncio.to_thread(_sha256, directory / "guard-proof.txt", 8192)
            if guard_hash != result.guard_proof_hash:
                return False
            receipt = json.loads(await asyncio.to_thread(
                (directory / "result.json").read_text, encoding="utf-8",
            ))
            for key in (
                "verification_id", "attempt_id", "run_id", "suite_ref", "status",
                "input_fingerprint", "suite_fingerprint", "report_hash", "guard_proof_hash",
                "exit_code",
            ):
                if receipt.get(key) != getattr(result, key):
                    return False
            parsed = parse_playwright_report(
                json.loads(await asyncio.to_thread(report.read_text, encoding="utf-8")),
                suite.expected_tests, result.exit_code if result.exit_code is not None else -1,
            )
            return parsed["status"] == result.status
        except (OSError, ValueError, KeyError, BrowserTestError):
            return False

    validate_evidence = revalidate
