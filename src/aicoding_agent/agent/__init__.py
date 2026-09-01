from typing import Any

__all__ = ["AgentLoop", "CompletionGate", "GateDecision"]


def __getattr__(name: str) -> Any:
    if name == "AgentLoop":
        from aicoding_agent.agent.loop import AgentLoop

        return AgentLoop
    if name in {"CompletionGate", "GateDecision"}:
        from aicoding_agent.agent.gate import CompletionGate, GateDecision

        return {"CompletionGate": CompletionGate, "GateDecision": GateDecision}[name]
    raise AttributeError(name)
