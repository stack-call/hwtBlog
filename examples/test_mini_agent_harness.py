"""No external dependencies. Run: python -m unittest -v test_mini_agent_harness.py"""
import asyncio
import unittest

from mini_agent_harness import AgentHarness, FakeModel, ModelOutput, Policy, ToolCall, ToolRegistry


def execute(awaitable):
    return asyncio.run(awaitable)


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry()

        async def read(_):
            return "refund logic found"

        self.registry.register("read_file", read, required_fields=("path",))

    def make(self, outputs, *, max_steps=5, registry=None, policy=None):
        return AgentHarness(
            model=FakeModel(outputs), registry=registry or self.registry,
            policy=policy or Policy(), max_steps=max_steps,
        )

    def test_two_tools_and_final(self):
        model = FakeModel([
            ModelOutput(tool_call=ToolCall("c1", "read_file", {"path": "coupon.py"})),
            ModelOutput(tool_call=ToolCall("c2", "read_file", {"path": "refund.py"})),
            ModelOutput(final="done"),
        ])
        agent = AgentHarness(model=model, registry=self.registry, policy=Policy())
        self.assertEqual(execute(agent.run("test")), "done")
        calls = [x["payload"].call_id for x in agent.events.records if x["kind"] == "tool_result"]
        self.assertEqual(calls, ["c1", "c2"])
        self.assertIn("tool_result", [x["kind"] for x in model.observed[-1]])

    def test_unknown_tool_fails_closed(self):
        a = self.make([ModelOutput(tool_call=ToolCall("u", "unknown", {})), ModelOutput(final="ok")])
        execute(a.run("test"))
        results = [r["payload"] for r in a.events.records if r["kind"] == "tool_result"]
        self.assertEqual(results[0].status, "failed")

    def test_missing_required_field(self):
        a = self.make([ModelOutput(tool_call=ToolCall("x", "read_file", {})), ModelOutput(final="ok")])
        execute(a.run("test"))
        results = [r["payload"] for r in a.events.records if r["kind"] == "tool_result"]
        self.assertEqual(results[0].content, "missing required fields")

    def test_risky_tool_denied_without_execution(self):
        calls = []

        async def dangerous(_):
            calls.append(1)
            return "paid"

        self.registry.register("refund_payment", dangerous, risky=True)
        a = self.make([ModelOutput(tool_call=ToolCall("r", "refund_payment", {})), ModelOutput(final="no")])
        execute(a.run("test"))
        self.assertFalse(calls)
        results = [r["payload"] for r in a.events.records if r["kind"] == "tool_result"]
        self.assertEqual(results[0].status, "denied")

    def test_tool_error_normalized(self):
        async def broken(_):
            raise ValueError("secret should not be logged")

        self.registry.register("broken", broken)
        a = self.make([ModelOutput(tool_call=ToolCall("e", "broken", {})), ModelOutput(final="ok")])
        execute(a.run("test"))
        results = [r["payload"] for r in a.events.records if r["kind"] == "tool_result"]
        self.assertEqual(results[0].status, "failed")
        self.assertNotIn("secret", results[0].content)

    def test_step_budget(self):
        a = self.make([ModelOutput(tool_call=ToolCall("1", "read_file", {"path": "a"}))], max_steps=1)
        with self.assertRaisesRegex(RuntimeError, "step budget"):
            execute(a.run("test"))
        self.assertEqual(a.events.records[-1]["kind"], "task_failed")

    def test_event_task_id(self):
        a = self.make([ModelOutput(final="done")])
        execute(a.run("test"))
        self.assertTrue(all(event["task_id"] == "coupon-fix-001" for event in a.events.records))


if __name__ == "__main__":
    unittest.main()
