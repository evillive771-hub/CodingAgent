from test_regression_gate import Budget, setup

from aicoding_agent.regression.gate import RegressionGate


async def test_red_report_is_revalidatable_without_allowing_failed_acceptance(tmp_path):
    candidate, _, suite, contract = setup(tmp_path)
    (candidate / "interface.py").write_text("VALUE = 2\n", encoding="utf-8")
    gate = RegressionGate(tmp_path / "evidence", command_suites=[suite])
    red = await gate.verify(contract, candidate, 0, "registry", "red", Budget())
    assert red.status == "assertion_failed"
    assert not await gate.revalidate(red, candidate, contract, 0, "registry")
    assert await gate.revalidate(red, candidate, contract, 0, "registry",
                                 allow_assertion_failure=True)
    (candidate / "interface.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert not await gate.revalidate(red, candidate, contract, 0, "registry",
                                     allow_assertion_failure=True)
