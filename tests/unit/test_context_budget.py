from aicoding_agent.context.budget import ContextBudgetConfig, ContextBudgetManager


def test_context_budget_is_token_and_output_reservation_aware() -> None:
    manager = ContextBudgetManager(
        ContextBudgetConfig(
            context_window_tokens=20_000,
            max_output_tokens=4_000,
            summary_output_tokens=3_000,
            compact_buffer_tokens=2_000,
            warning_buffer_tokens=3_000,
        )
    )

    assert manager.config.effective_context_tokens == 17_000
    assert manager.config.warning_threshold_tokens == 14_000
    assert manager.config.compact_threshold_tokens == 15_000

    manager.observe_usage({"prompt_tokens": 12_345})
    status = manager.status([{"role": "user", "content": "你好 world"}])
    assert status.last_prompt_tokens == 12_345
    assert status.estimated_tokens > 0


def test_cjk_estimation_is_more_conservative_than_ascii() -> None:
    manager = ContextBudgetManager()
    chinese = manager.estimate_messages([{"role": "user", "content": "测" * 100}])
    ascii_text = manager.estimate_messages([{"role": "user", "content": "a" * 100}])

    assert chinese > ascii_text
