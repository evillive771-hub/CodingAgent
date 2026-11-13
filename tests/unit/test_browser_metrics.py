from aicoding_agent.agent.observability import TelemetryRegistry, duration_percentiles


def test_browser_latency_uses_milliseconds_and_distinguishes_single_measurement() -> None:
    registry = TelemetryRegistry()
    registry.observe(
        "browser-run",
        "browser.verification_finished",
        {"status": "failed", "duration_seconds": 0.1},
    )
    first = registry.get("browser-run")
    assert first is not None
    assert duration_percentiles(first.browser_duration_ms) == {"n": 1, "duration_ms": 100}
    registry.observe(
        "browser-run",
        "browser.verification_finished",
        {"status": "passed", "duration_seconds": 0.3},
    )
    registry.observe(
        "browser-run",
        "browser.verification_finished",
        {"status": "timeout", "duration_ms": 500},
    )
    final = registry.get("browser-run")
    assert final is not None
    assert final.browser_runs == 3
    assert final.browser_failures == 1
    assert final.browser_infra_failures == 1
    assert duration_percentiles(final.browser_duration_ms) == {
        "n": 3,
        "p50_ms": 300,
        "p90_ms": 460,
        "p95_ms": 480,
    }
    assert duration_percentiles([]) == {"n": 0}
