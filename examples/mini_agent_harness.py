"""Educational Agent Harness core; deterministic and free of external effects.

Run: python mini_agent_harness.py
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelOutput:
    tool_call: Optional[ToolCall] = None
    final: Optional[str] = None

    def __post_init__(self):
        if (self.tool_call is None) == (self.final is None):
            raise ValueError("ModelOutput must contain exactly one of tool_call or final")


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    status: str
    content: str


@dataclass
class EventStore:
    task_id: str
    records: list[dict[str, Any]] = field(default_factory=list)

    def append(self, kind: str, payload: Any) -> None:
        self.records.append({"task_id": self.task_id, "kind": kind, "payload": payload})

    def snapshot(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.records)


ToolFn = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class RegisteredTool:
    execute: ToolFn
    required_fields: frozenset[str]
    risky: bool = False


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self, name: str, fn: ToolFn, *, required_fields: tuple[str, ...] = (), risky: bool = False
    ) -> None:
        if name in self._tools:
            raise ValueError(f"duplicate tool: {name}")
        self._tools[name] = RegisteredTool(fn, frozenset(required_fields), risky)

    def get(self, name: str) -> Optional[RegisteredTool]:
        return self._tools.get(name)


class Policy:
    """Default-deny risky actions in this educational implementation."""

    def __init__(self, allow_risky: bool = False) -> None:
        self.allow_risky = allow_risky

    def permits(self, tool: RegisteredTool) -> bool:
        return not tool.risky or self.allow_risky


class FakeModel:
    """Scripted fake model, one completed output per request."""

    def __init__(self, outputs: list[ModelOutput]) -> None:
        self.outputs = list(outputs)
        self.observed: list[tuple[dict[str, Any], ...]] = []

    async def next(self, events: tuple[dict[str, Any], ...]) -> ModelOutput:
        self.observed.append(events)
        if not self.outputs:
            raise RuntimeError("fake model exhausted")
        return self.outputs.pop(0)


class AgentHarness:
    def __init__(
        self, *, model: FakeModel, registry: ToolRegistry, policy: Policy,
        task_id: str = "coupon-fix-001", max_steps: int = 5,
    ) -> None:
        self.model = model
        self.registry = registry
        self.policy = policy
        self.events = EventStore(task_id)
        self.max_steps = max_steps

    async def dispatch(self, call: ToolCall) -> ToolResult:
        self.events.append("tool_started", call)
        tool = self.registry.get(call.name)
        if tool is None:
            result = ToolResult(call.call_id, "failed", "unknown tool")
        elif not tool.required_fields.issubset(call.arguments):
            result = ToolResult(call.call_id, "failed", "missing required fields")
        elif not self.policy.permits(tool):
            result = ToolResult(call.call_id, "denied", "policy denied risky tool")
        else:
            try:
                content = await tool.execute(call.arguments)
                result = ToolResult(call.call_id, "completed", str(content))
            except Exception as e:
                result = ToolResult(call.call_id, "failed", f"tool error: {type(e).__name__}")
        self.events.append("tool_finished", result)
        return result

    async def run(self, task: str) -> str:
        self.events.append("task_started", task)
        for _ in range(self.max_steps):
            self.events.append("model_requested", {"step": len(self.model.observed) + 1})
            try:
                output = await self.model.next(self.events.snapshot())
            except Exception as e:
                self.events.append("task_failed", type(e).__name__)
                raise
            self.events.append("model_responded", output)
            if output.final is not None:
                self.events.append("task_completed", output.final)
                return output.final
            if output.tool_call is None:
                raise AssertionError("invalid model output")
            self.events.append("tool_result", await self.dispatch(output.tool_call))
        self.events.append("task_failed", "step budget exceeded")
        raise RuntimeError("step budget exceeded")


async def _example() -> None:
    async def read_file(args: dict[str, Any]) -> str:
        return "refund = order.original_amount"

    async def run_test(args: dict[str, Any]) -> str:
        return "1 failed: expected 80, got 100"

    registry = ToolRegistry()
    registry.register("read_file", read_file, required_fields=("path",))
    registry.register("run_refund_test", run_test)
    model = FakeModel([
        ModelOutput(tool_call=ToolCall("c1", "read_file", {"path": "refund.py"})),
        ModelOutput(tool_call=ToolCall("c2", "run_refund_test", {})),
        ModelOutput(final="定位到原价退款错误，测试仍失败，需要进一步修改代码。"),
    ])
    agent = AgentHarness(model=model, registry=registry, policy=Policy())
    print(await agent.run("修复优惠券退款 Bug"))
    print(f"events={len(agent.events.records)}")


if __name__ == "__main__":
    asyncio.run(_example())
