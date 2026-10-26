---
title: 第十九章：从零实现一个最小 Agent Harness
sidebar_label: 第十九章：最小 Harness
description: 以优惠券测试为例整合 Loop、工具、策略、执行、适配、事件与验证
sidebar_position: 20
tags: [Agent, Harness, Python, Architecture]
---


# 第十九章：从零实现一个 Agent Harness

## 一、这一次，我们究竟要造什么？

经过前面十八章，我们已经理解了很多模块：

- Agent Loop
- Tool System
- Execution Environment
- Permission & Approval
- Planning
- Human-in-the-Loop
- Model Adapter
- Context Management
- Session
- Observability

但如果现在有人问：

> 一个 Agent Harness 真正运行起来的时候，这些模块到底是谁先调用谁？

你可能仍然不容易画出一条完整调用链。

所以这一章，我们不再单独介绍某个模块，而是亲手搭建一套最小系统。

继续使用熟悉的优惠券退款场景。

用户要求：

> 修复部分退款金额计算错误，并运行测试确认结果。

项目中存在一个 Bug：

```python
def refund(paid_total, quantity, refunded_quantity):
    return round(paid_total / quantity * refunded_quantity)
```

假设用户实际支付 85 元，购买两件商品，退回一件。

期望退款：

```text
42.50 元
```

但当前代码执行：

```python
round(85 / 2 * 1)
```

得到：

```text
42
```

原因是没有保留两位小数。

我们希望 Agent 完成这样一条执行链：

```text
用户提出任务
      |
      v
读取 refund.py
      |
      v
运行测试，发现失败
      |
      v
提出代码修改
      |
      v
等待修改审批
      |
      v
用户批准
      |
      v
修改 refund.py
      |
      v
重新运行测试
      |
      v
确认通过并结束
```

注意，我们不会把这些操作直接写成固定业务脚本。

我们要构建的是一套通用执行机制。

它应该允许**模型不断提出下一步工具请求**，而由 Harness 控制具体执行。

---

## 二、先想象完全没有 Harness 会怎样

假设我们只有一个 LLM。

```python
response = llm.chat(
    "修复部分退款 Bug，并运行测试"
)
```

模型可能回答：

```text
应该将 round() 改为保留两位小数。
```

但代码没有改变。

所以我们需要让模型提出工具调用。

模型返回：

```json
{
  "name": "read_file",
  "arguments": {
    "path": "refund.py"
  }
}
```

我们执行：

```python
read_file("refund.py")
```

然后把结果交回模型。

随后模型可能提出：

```json
{
  "name": "run_tests",
  "arguments": {}
}
```

再执行测试。

这时我们已经推导出了最小循环：

```python
while True:
    reply = model.generate(messages)

    if not reply.tool_calls:
        break

    for call in reply.tool_calls:
        result = execute_tool(call)
        messages.append(result)
```

但这段代码里有四个明显的问题。

**第一，`execute_tool()` 怎么知道有哪些工具？**

**第二，模型传错参数或者访问其他用户文件怎么办？**

**第三，修改文件时需要审批，如何让 Loop 暂停而不是直接继续？**

**第四，工具执行完成后，下一次模型调用究竟能看到哪些内容？**

我们现在就围绕这四个问题，逐渐搭建系统。

---

## 三、先把几个最重要的对象分开

我们先不实现全部业务逻辑。

只定义几个核心对象。

### 3.1 ToolCall：模型想做什么

```python
@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict
```

例如：

```python
ToolCall(
    id="call-1",
    name="read_file",
    args={"path": "refund.py"},
)
```

这是模型产生的**行动请求**。

它不代表工具已经执行。

### 3.2 ModelReply：模型本轮返回了什么

```python
@dataclass(frozen=True)
class ModelReply:
    text: str = ""
    calls: tuple[ToolCall, ...] = ()
```

一次模型回复可能只包含文字。

也可能包含一个或多个工具调用。

### 3.3 Session：当前任务已经经历了什么

```python
@dataclass
class Session:
    id: str
    messages: list[dict]
    pending: list[ToolCall]
```

我们把两个东西分开。

`messages` 是已经记录的交互历史。

`pending` 是已经产生、但尚未全部处理完的工具请求。

为什么需要 `pending`？

因为执行可能中途被打断。

例如：

```text
模型请求修改文件
       |
       v
Permission 返回 Ask
       |
       v
等待用户批准
```

此时工具请求还没有执行，但又不能直接丢掉。

所以必须保留一个明确的待处理状态。

### 3.4 Tool Registry：工具有哪些

我们用一个注册表维护：

```text
read_file
replace_text
run_tests
```

Registry 不负责决定下一步应该执行什么。

它负责回答：

> 模型请求的工具是否存在？它要求哪些参数？具体执行函数是什么？

### 3.5 Dispatcher：把行动请求转成实际执行

Dispatcher 的职责是：

```text
ToolCall
   |
   v
Registry 查找工具
   |
   v
参数验证
   |
   v
Permission Check
   |
   v
Execution Backend
   |
   v
Tool Result
```

Agent Loop 不应该自己知道如何读取文件或启动测试进程。

这些由 Dispatcher 和 Backend 负责。

### 3.6 Context Builder：决定模型看见什么

对于本章的最小实现，它只做：

```text
System Instruction
+
Session Messages
```

然后检查一个简化的上下文长度限制。

真正的 Harness 还会加入：

```text
Compaction
Memory
Skills
Tool Result Offloading
Token Budget
```

但我们暂时不实现它们。

这里最重要的是把职责边界留出来。

---

## 四、完整的运行调用链

现在把上述对象连在一起。

```text
用户任务
   |
   v
Session
   |
   v
AgentHarness.run()
   |
   v
ContextBuilder.build()
   |
   v
ModelAdapter.generate()
   |
   v
ModelReply
   |
   +-----------------------+
   |                       |
   v                       v
Tool Calls               Final Text
   |                       |
   v                       v
Session.pending          Turn Completed
   |
   v
Tool Dispatcher
   |
   +--> Tool Registry
   |
   +--> Input Validation
   |
   +--> Permission Policy
   |          |
   |          +--> Approval Required
   |                     |
   |                     v
   |                Pause Turn
   |
   v
Execution Backend
   |
   v
Tool Result
   |
   v
Session.messages
   |
   v
ContextBuilder
   |
   v
下一次 Model Request
```

这张图就是我们要实现的最小 Agent Harness。

请特别注意：

**Agent Loop 不是所有功能的集合，而是这些组件的调度者。**

---

## 五、先看完整工程结构

我们使用 Python 标准库，不需要安装 LangChain、OpenAI SDK 或其他第三方依赖。

最终项目结构：

```text
mini-agent-harness/
│
├── harness.py
│   ├── ToolCall / ModelReply
│   ├── Session
│   ├── EventRecorder
│   ├── ContextBuilder
│   ├── ToolRegistry
│   ├── Permission Policy
│   ├── LocalBackend
│   ├── Dispatcher
│   ├── ScriptedModel
│   └── AgentHarness
│
├── demo.py
│
└── test_harness.py
```

`harness.py` 是通用执行框架。

`demo.py` 提供优惠券退款场景。

`test_harness.py` 验证框架是否符合预期。

**安全说明：这里的 LocalBackend 没有操作系统 Sandbox。它只用于执行我们自己创建的可信测试项目，不能用于运行陌生仓库中的代码。**

---

## 六、第一部分：实现 Harness 核心

创建 `harness.py`。

下面代码可以完整复制。

```python
"""最小同步 Agent Harness。

仅用于可信的教学测试项目。
不是安全沙箱，也不是生产级 Agent Runtime。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import hashlib
import json
import subprocess
import sys


# ==================================================
# 1. Model / Session 数据结构
# ==================================================

@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass(frozen=True)
class ModelReply:
    text: str = ""
    calls: tuple[ToolCall, ...] = ()


@dataclass
class Session:
    id: str
    messages: list[dict] = field(default_factory=list)
    pending: list[ToolCall] = field(default_factory=list)


# ==================================================
# 2. Observability
# ==================================================

class EventRecorder:
    def __init__(self):
        self.events: list[dict] = []

    def emit(self, event: str, session: Session, **fields):
        self.events.append({
            "event": event,
            "session_id": session.id,
            **fields,
        })


# ==================================================
# 3. Context Builder
# ==================================================

class ContextBuilder:
    def __init__(self, max_chars: int = 100_000):
        self.max_chars = max_chars

    def build(self, session: Session) -> list[dict]:
        result = [
            {
                "role": "system",
                "content": (
                    "Fix the refund bug and verify tests."
                ),
            },
            *session.messages,
        ]

        size = len(json.dumps(
            result,
            ensure_ascii=False,
        ))

        if size > self.max_chars:
            raise ValueError(
                "Context budget exceeded; "
                "compaction is not implemented"
            )

        return result


# ==================================================
# 4. Tool Definition
# ==================================================

@dataclass
class Tool:
    name: str
    description: str
    required: dict[str, type]
    execute: Callable[[dict, Path], str]

    def definition(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    key: {"type": "string"}
                    for key in self.required
                },
                "required": list(self.required),
                "additionalProperties": False,
            },
        }

    def check(self, args: dict):
        if (
            not isinstance(args, dict)
            or set(args) != set(self.required)
        ):
            raise ValueError(
                "unexpected or missing parameters"
            )

        for key, expected in self.required.items():
            if type(args[key]) is not expected:
                raise ValueError(
                    f"{key} must be {expected.__name__}"
                )


# ==================================================
# 5. Tool Registry
# ==================================================

class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        names = [tool.name for tool in tools]

        if len(set(names)) != len(names):
            raise ValueError("duplicate tools")

        self.tools = {
            tool.name: tool
            for tool in tools
        }

    def definitions(self):
        return [
            tool.definition()
            for tool in self.tools.values()
        ]


# ==================================================
# 6. Permission / Approval
# ==================================================

class ApprovalRequired(Exception):
    def __init__(self, call: ToolCall):
        super().__init__(
            f"Approval required: {call.name} {call.args}"
        )
        self.call = call


def call_fingerprint(
    session: Session,
    call: ToolCall,
    root: Path,
) -> str:
    data = json.dumps({
        "session_id": session.id,
        "root": str(root.resolve()),
        "id": call.id,
        "name": call.name,
        "args": call.args,
    }, sort_keys=True)

    return hashlib.sha256(
        data.encode()
    ).hexdigest()


class Policy:
    def __init__(self):
        self.grants: set[str] = set()

    def grant(
        self,
        session: Session,
        call: ToolCall,
        root: Path,
    ):
        # 教学中由可信调用者模拟用户批准。
        self.grants.add(
            call_fingerprint(session, call, root)
        )

    def check(
        self,
        session: Session,
        call: ToolCall,
        root: Path,
    ):
        # 修改文件需要明确授权。
        if call.name == "replace_text":
            fingerprint = call_fingerprint(
                session, call, root
            )

            if fingerprint not in self.grants:
                raise ApprovalRequired(call)


# ==================================================
# 7. Execution Backend
# ==================================================

class LocalBackend:
    """只能用于可信代码。路径校验不是安全沙箱。"""

    @staticmethod
    def resolve(root: Path, path: str) -> Path:
        candidate = (root / path).resolve()

        if (
            not candidate.is_relative_to(root.resolve())
            or not candidate.is_file()
        ):
            raise ValueError("invalid workspace path")

        return candidate

    @staticmethod
    def run_tests(root: Path) -> str:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-v",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        output = (
            result.stdout + result.stderr
        )[-6000:]

        return (
            f"exit_code={result.returncode}\n"
            f"{output}"
        )


# ==================================================
# 8. 创建实际可执行工具
# ==================================================

def make_registry() -> ToolRegistry:
    backend = LocalBackend()

    def read_file(args: dict, root: Path) -> str:
        path = backend.resolve(
            root, args["path"]
        )

        return path.read_text(
            encoding="utf-8"
        )

    def replace_text(args: dict, root: Path) -> str:
        path = backend.resolve(
            root, args["path"]
        )

        original = path.read_text(
            encoding="utf-8"
        )

        old = args["old"]
        new = args["new"]

        if (
            not old
            or original.count(old) != 1
        ):
            raise ValueError(
                "replacement anchor "
                "must match exactly once"
            )

        path.write_text(
            original.replace(old, new),
            encoding="utf-8",
        )

        return "replacement applied"

    def run_tests(args: dict, root: Path) -> str:
        return backend.run_tests(root)

    return ToolRegistry([
        Tool(
            "read_file",
            "Read a workspace file",
            {"path": str},
            read_file,
        ),
        Tool(
            "replace_text",
            "Replace one exact occurrence",
            {
                "path": str,
                "old": str,
                "new": str,
            },
            replace_text,
        ),
        Tool(
            "run_tests",
            "Run trusted unittest tests",
            {},
            run_tests,
        ),
    ])


# ==================================================
# 9. Tool Dispatcher
# ==================================================

class Dispatcher:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: Policy,
        events: EventRecorder,
        root: Path,
    ):
        self.registry = registry
        self.policy = policy
        self.events = events
        self.root = root

    def dispatch(
        self,
        session: Session,
        call: ToolCall,
    ) -> dict:
        self.events.emit(
            "tool.started",
            session,
            call_id=call.id,
            tool=call.name,
        )

        try:
            tool = self.registry.tools.get(
                call.name
            )

            if tool is None:
                raise ValueError("unknown tool")

            # 先验证参数
            tool.check(call.args)

            # 再检查权限
            self.policy.check(
                session,
                call,
                self.root,
            )

            # 最后执行
            output = tool.execute(
                call.args,
                self.root,
            )

            error = False

        except ApprovalRequired:
            self.events.emit(
                "approval.required",
                session,
                call_id=call.id,
            )

            # 必须交还 Agent Loop 处理暂停
            raise

        except Exception as exc:
            # 普通工具错误转成 Tool Result
            output = (
                f"{type(exc).__name__}: {exc}"
            )
            error = True

        self.events.emit(
            "tool.completed",
            session,
            call_id=call.id,
            is_error=error,
        )

        return {
            "role": "tool",
            "tool_call_id": call.id,
            "content": output,
            "is_error": error,
        }


# ==================================================
# 10. Scripted Model Adapter
# ==================================================

class ScriptedModel:
    """只模拟模型决策，工具执行全部是真实的。"""

    def __init__(
        self,
        replies: list[ModelReply],
    ):
        self.replies = iter(replies)
        self.requests: list[list[dict]] = []

    def generate(
        self,
        messages: list[dict],
        tools: list[dict],
    ) -> ModelReply:
        self.requests.append(messages)

        return next(self.replies)


# ==================================================
# 11. Agent Loop
# ==================================================

class AgentHarness:
    def __init__(
        self,
        model,
        registry,
        dispatcher,
        events,
        context=None,
    ):
        self.model = model
        self.registry = registry
        self.dispatcher = dispatcher
        self.events = events

        self.context = (
            context or ContextBuilder()
        )

    def run(
        self,
        session: Session,
        user_text: str | None = None,
        max_steps: int = 12,
    ) -> str:

        if user_text is not None:
            if session.pending:
                raise ValueError(
                    "finish pending tool calls "
                    "before a new user turn"
                )

            session.messages.append({
                "role": "user",
                "content": user_text,
            })

        for step in range(max_steps):

            # 有待处理工具时，先处理工具。
            # 这样审批恢复后不会重复调用模型。
            if not session.pending:
                self.events.emit(
                    "model.started",
                    session,
                    step=step,
                )

                messages = self.context.build(
                    session
                )

                reply = self.model.generate(
                    messages,
                    self.registry.definitions(),
                )

                self.events.emit(
                    "model.completed",
                    session,
                    step=step,
                )

                # 先记录 Assistant 的 Tool Calls
                session.messages.append({
                    "role": "assistant",
                    "content": reply.text,
                    "tool_calls": [
                        vars(call)
                        for call in reply.calls
                    ],
                })

                if not reply.calls:
                    self.events.emit(
                        "turn.completed",
                        session,
                    )
                    return "completed"

                session.pending.extend(
                    reply.calls
                )

            # 逐个执行本次模型提出的工具
            while session.pending:
                call = session.pending[0]

                try:
                    result = (
                        self.dispatcher.dispatch(
                            session,
                            call,
                        )
                    )

                except ApprovalRequired:
                    # 工具请求仍保留在 pending
                    return "waiting_approval"

                # Tool Result 进入历史
                session.messages.append(result)

                # 执行完才删除 pending
                session.pending.pop(0)

        self.events.emit(
            "turn.step_limit",
            session,
        )

        return "step_limit"
```

代码虽然不短，但所有模块的职责已经分开了。

`AgentHarness` 没有读取文件或运行 Shell 的代码。

`ToolRegistry` 不会调用模型。

`ContextBuilder` 不知道怎么执行工具。

`Policy` 也不负责修改代码。

这正是我们要实现的架构边界。

---

## 七、第二部分：让 Agent 真正修复优惠券 Bug

现在创建 `demo.py`。

我们使用 Python 标准库创建一个临时项目。

项目里有：

```text
refund.py
test_refund.py
```

初始代码故意存在一个 Bug。

然后我们让 `ScriptedModel` 依次产生五次模型回复。

为什么这里不直接接真实 LLM？

因为我们正在验证 Harness。

如果一边开发 Runtime，一边让真实模型随机选择工具，就很难判断失败到底来自 Runtime 还是模型决策。

**先固定模型输出，才能可靠测试 Harness 的控制流。**

之后再把 ScriptedModel 换成真实 Model Adapter。

### demo.py

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from harness import (
    AgentHarness,
    Dispatcher,
    EventRecorder,
    ModelReply,
    Policy,
    ScriptedModel,
    Session,
    ToolCall,
    make_registry,
)


BUG = (
    "return round("
    "paid_total / quantity * refunded_quantity)"
)

FIX = (
    "return round("
    "paid_total * refunded_quantity / quantity, 2)"
)


def prepare(root: Path):
    # 创建包含 Bug 的项目文件
    (root / "refund.py").write_text(
        "def refund("
        "paid_total, quantity, refunded_quantity"
        "):\n"
        f"    {BUG}\n",
        encoding="utf-8",
    )

    # 创建回归测试
    (root / "test_refund.py").write_text(
        "import unittest\n"
        "from refund import refund\n"
        "class RefundTest(unittest.TestCase):\n"
        "    def test_partial_refund(self):\n"
        "        self.assertEqual("
        "refund(85, 2, 1), 42.50)\n",
        encoding="utf-8",
    )


def scenario():
    # 需要审批的精确补丁
    edit = ToolCall(
        "call-3",
        "replace_text",
        {
            "path": "refund.py",
            "old": BUG,
            "new": FIX,
        },
    )

    # 模拟模型连续五轮做出的决定
    replies = [
        # 第 1 轮：读取源码
        ModelReply(calls=(
            ToolCall(
                "call-1",
                "read_file",
                {"path": "refund.py"},
            ),
        )),

        # 第 2 轮：运行测试
        ModelReply(calls=(
            ToolCall(
                "call-2",
                "run_tests",
                {},
            ),
        )),

        # 第 3 轮：请求修改代码
        ModelReply(calls=(edit,)),

        # 第 4 轮：再次运行测试
        ModelReply(calls=(
            ToolCall(
                "call-4",
                "run_tests",
                {},
            ),
        )),

        # 第 5 轮：给出最终答复
        ModelReply(
            text="已修复部分退款精度问题，测试通过。"
        ),
    ]

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        prepare(root)

        events = EventRecorder()
        policy = Policy()
        registry = make_registry()
        model = ScriptedModel(replies)

        dispatcher = Dispatcher(
            registry,
            policy,
            events,
            root,
        )

        harness = AgentHarness(
            model,
            registry,
            dispatcher,
            events,
        )

        session = Session("refund-001")

        # 第一次运行
        status = harness.run(
            session,
            "修复部分退款 Bug，并运行测试",
        )

        # 修改文件前必须暂停
        assert status == "waiting_approval"

        # 此时文件确实还没有改动
        assert BUG in (
            root / "refund.py"
        ).read_text()

        assert session.pending == [edit]

        print(
            "审批前:",
            status,
            "文件未改动",
        )

        # 模拟可信外部审批：
        # 用户已看到这个精确补丁，并明确批准。
        policy.grant(
            session,
            edit,
            root,
        )

        # 恢复同一个 Session
        status = harness.run(session)

        assert status == "completed"

        # 真实文件已经被修改
        assert FIX in (
            root / "refund.py"
        ).read_text()

        # 第一次测试失败
        assert "exit_code=1" in str(
            session.messages
        )

        # 修改之后测试成功
        assert "exit_code=0" in str(
            session.messages
        )

        print("审批后:", status)

        results = [
            m["content"].splitlines()[0]
            for m in session.messages
            if (
                m["role"] == "tool"
                and "exit_code=" in m["content"]
            )
        ]

        print("测试结果:", results)
        print(
            "模型调用次数:",
            len(model.requests),
        )
        print(
            "事件类型:",
            [
                event["event"]
                for event in events.events
            ],
        )


if __name__ == "__main__":
    scenario()
```

运行：

```bash
python demo.py
```

运行结果：

```text
审批前: waiting_approval 文件未改动

审批后: completed

测试结果: ['exit_code=1', 'exit_code=0']

模型调用次数: 5
```

这份输出已经在本地实际验证过。

我们现在分析它为什么重要。

---

## 八、最关键的一步：审批发生时，Agent Loop 到底处于什么状态？

这是整套 Harness 中最值得理解的地方。

第三次模型请求返回：

```python
ToolCall(
    id="call-3",
    name="replace_text",
    args={
        "path": "refund.py",
        "old": BUG,
        "new": FIX,
    },
)
```

此时，Agent Loop 先执行：

```python
session.messages.append({
    "role": "assistant",
    "tool_calls": [...],
})
```

然后：

```python
session.pending.extend(reply.calls)
```

现在 Session 状态大致是：

```text
messages:
  User 请求
  Assistant read_file
  Tool read_file result
  Assistant run_tests
  Tool run_tests result
  Assistant replace_text

pending:
  call-3 replace_text
```

注意：

**工具请求已经被记录，但还没有产生 Tool Result。**

接下来进入 Dispatcher。

```python
tool.check(call.args)
```

参数合法。

然后：

```python
policy.check(session, call, root)
```

由于修改文件需要审批，Policy 抛出：

```python
ApprovalRequired(call)
```

Agent Loop 捕获后：

```python
except ApprovalRequired:
    return "waiting_approval"
```

于是任务暂停。

此时系统有两个明确事实：

1. 修改动作还没有执行。
2. 修改请求仍保存在 `session.pending` 中。

等用户批准后：

```python
policy.grant(session, edit, root)
```

再调用：

```python
harness.run(session)
```

注意，我们没有传入新的用户任务。

恢复时会先检查：

```python
if not session.pending:
    reply = model.generate(...)
```

由于 `pending` 不为空，这一次会直接处理之前留下的工具调用。

**因此不会再次请求模型生成同一个补丁。**

执行成功后：

```python
session.messages.append(result)
session.pending.pop(0)
```

工具结果进入历史。

待处理工具被移除。

随后 Agent Loop 才进入下一轮模型调用。

这就是最小的**暂停与恢复控制流**。

但要记住：它只在当前 Python 进程的内存中保留状态。进程重启后不能恢复，真实系统还需要持久化 Checkpoint。

---

## 九、为什么 Tool Result 必须再交回模型？

我们现在研究另一处关键代码：

```python
session.messages.append(result)
```

假设这次执行的是：

```text
run_tests
```

返回：

```text
exit_code=1

FAIL test_partial_refund
Expected: 42.50
Actual: 42
```

Tool Runtime 没有直接决定重新修改代码。

它只是记录：

```python
{
    "role": "tool",
    "tool_call_id": "call-2",
    "content": "exit_code=1 ...",
    "is_error": False,
}
```

为什么 `is_error` 是 False？

因为**测试进程正常启动并结束了，只是测试断言失败**。

这是环境反馈，不是工具分发异常。

下一次：

```python
ContextBuilder.build(session)
```

会把 Tool Result 加入模型输入。

模型再根据测试失败的信息决定下一步行动。

我们刚才使用的 ScriptedModel 已经预先规定了下一步。

真实 LLM 则会根据实际上下文生成新的决定。

这就是：

```text
Model Decision
      |
      v
Tool Execution
      |
      v
Environment Feedback
      |
      v
Context Builder
      |
      v
Next Model Decision
```

**Agent 的闭环并不是围绕工具运转，而是围绕“决策—执行—反馈”运转。**

---

## 十、第三部分：给 Harness 增加测试

我们不能只看 Demo 成功。

还要验证发生异常时，系统有没有保持正确的控制流。

创建 `test_harness.py`。

```python
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory

from harness import (
    Tool,
    ToolCall,
    ToolRegistry,
    Policy,
    Dispatcher,
    EventRecorder,
    Session,
    AgentHarness,
    ModelReply,
    ScriptedModel,
    ContextBuilder,
    make_registry,
)


class HarnessTests(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

        (self.root / "a.txt").write_text(
            "hello"
        )

        self.events = EventRecorder()
        self.policy = Policy()
        self.registry = make_registry()

        self.dispatcher = Dispatcher(
            self.registry,
            self.policy,
            self.events,
            self.root,
        )

        self.session = Session("test")

    def tearDown(self):
        self.tmp.cleanup()

    def test_unknown_tool_returned_as_result(self):
        call = ToolCall(
            "1", "unknown", {}
        )

        out = self.dispatcher.dispatch(
            self.session, call
        )

        self.assertTrue(out["is_error"])

        self.assertIn(
            "unknown tool",
            out["content"],
        )

    def test_bad_args_do_not_execute(self):
        call = ToolCall(
            "2",
            "read_file",
            {"filepath": "a.txt"},
        )

        out = self.dispatcher.dispatch(
            self.session, call
        )

        self.assertTrue(out["is_error"])

    def test_path_escape_rejected(self):
        call = ToolCall(
            "3",
            "read_file",
            {"path": "../nope"},
        )

        out = self.dispatcher.dispatch(
            self.session, call
        )

        self.assertTrue(out["is_error"])

    def test_approval_resume_and_binding(self):
        call = ToolCall(
            "4",
            "replace_text",
            {
                "path": "a.txt",
                "old": "hello",
                "new": "world",
            },
        )

        model = ScriptedModel([
            ModelReply(calls=(call,)),
            ModelReply(text="done"),
        ])

        agent = AgentHarness(
            model,
            self.registry,
            self.dispatcher,
            self.events,
        )

        status = agent.run(
            self.session, "fix"
        )

        self.assertEqual(
            status,
            "waiting_approval",
        )

        self.assertEqual(
            (self.root / "a.txt").read_text(),
            "hello",
        )

        # 批准一个不同的补丁：
        # 不应该解锁原来的请求
        changed = ToolCall(
            "4",
            "replace_text",
            {
                "path": "a.txt",
                "old": "hello",
                "new": "OTHER",
            },
        )

        self.policy.grant(
            self.session,
            changed,
            self.root,
        )

        self.assertEqual(
            agent.run(self.session),
            "waiting_approval",
        )

        # 批准原始精确操作
        self.policy.grant(
            self.session,
            call,
            self.root,
        )

        self.assertEqual(
            agent.run(self.session),
            "completed",
        )

        self.assertEqual(
            (self.root / "a.txt").read_text(),
            "world",
        )

        # 审批恢复不重复调用模型
        self.assertEqual(
            len(model.requests),
            2,
        )

    def test_context_limit_is_not_silent_truncation(self):
        agent = AgentHarness(
            ScriptedModel([]),
            self.registry,
            self.dispatcher,
            self.events,
            ContextBuilder(max_chars=5),
        )

        with self.assertRaisesRegex(
            ValueError,
            "Context budget",
        ):
            agent.run(self.session, "fix")

    def test_step_limit(self):
        model = ScriptedModel([
            ModelReply(calls=(
                ToolCall(
                    "a",
                    "read_file",
                    {"path": "a.txt"},
                ),
            )),
            ModelReply(text="done"),
        ])

        agent = AgentHarness(
            model,
            self.registry,
            self.dispatcher,
            self.events,
        )

        self.assertEqual(
            agent.run(
                self.session,
                "fix",
                max_steps=1,
            ),
            "step_limit",
        )

        self.assertEqual(
            agent.run(
                self.session,
                max_steps=1,
            ),
            "completed",
        )

    def test_duplicate_tools(self):
        tool = Tool(
            "foo",
            "desc",
            {},
            lambda args, root: "ok",
        )

        with self.assertRaisesRegex(
            ValueError,
            "duplicate",
        ):
            ToolRegistry([tool, tool])


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

运行：

```bash
python -m unittest -v test_harness.py
```

实际验证结果：

```text
test_approval_resume_and_binding ... ok
test_bad_args_do_not_execute ... ok
test_context_limit_is_not_silent_truncation ... ok
test_duplicate_tools ... ok
test_path_escape_rejected ... ok
test_step_limit ... ok
test_unknown_tool_returned_as_result ... ok

Ran 7 tests

OK
```

### 为什么重点测试这些场景？

因为它们覆盖的不是业务公式，而是 Harness 的执行不变量。

| 测试 | 验证的系统性质 |
|---|---|
| Unknown Tool | 不存在的工具不会被错误执行 |
| Invalid Arguments | 不合法参数不会进入工具实现 |
| Path Escape | 文件工具限制到指定工作区路径 |
| Approval Binding | 批准不同参数不能解锁原调用 |
| Resume | 审批后能继续未完成的 Tool Call |
| Context Limit | 超限时不会悄悄删除历史 |
| Step Limit | 连续模型调用受到步数上限约束 |
| Duplicate Registration | 不允许工具名称冲突 |

注意，路径检查只是基础防护，不是安全沙箱。测试通过也不能证明能够安全运行不可信代码。

---

## 十一、现在真正理解每个模块为什么存在

回头看最初的问题：

> 模型请求修改 refund.py，怎么变成真实的文件修改？

我们现在可以沿着代码回答。

### 第一步：Model Adapter 产生行动请求

```python
reply = model.generate(
    messages,
    tool_definitions,
)
```

返回：

```python
ToolCall(
    id="call-3",
    name="replace_text",
    args={...},
)
```

模型不直接访问文件系统。

### 第二步：Session 保存未处理请求

```python
session.pending.extend(reply.calls)
```

这让工具执行过程可以暂停。

### 第三步：Registry 查找工具

```python
tool = registry.tools.get(call.name)
```

Agent Loop 不必知道 `replace_text` 的具体实现。

### 第四步：验证输入

```python
tool.check(call.args)
```

防止参数不符合工具契约。

### 第五步：检查权限

```python
policy.check(session, call, root)
```

这次修改需要授权。

### 第六步：Execution Backend 修改文件

获得授权后：

```python
tool.execute(call.args, root)
```

才真正产生副作用。

### 第七步：记录结果

```python
session.messages.append(result)
```

让模型在后续请求中看到执行反馈。

### 第八步：Event Recorder 保存运行事实

```python
events.emit(
    "tool.completed",
    session,
    ...
)
```

方便定位执行过程中发生了什么。

至此，一次工具调用才真正完成。

---

## 十二、如果把 ScriptedModel 替换成真实 LLM，需要修改什么？

现在我们已经证明 Harness 的核心控制流能够运行。

但 `ScriptedModel` 并不理解代码，它只是按预先写好的顺序返回工具调用。

因此，下一步不是修改 Agent Loop，而是实现真实的 Model Adapter。

例如：

```python
class RealModelAdapter:

    def generate(
        self,
        messages,
        tools,
    ) -> ModelReply:

        # 1. 转换内部消息
        provider_messages = encode_messages(
            messages
        )

        # 2. 转换工具定义
        provider_tools = encode_tools(
            tools
        )

        # 3. 请求真实模型
        raw_response = call_provider(
            provider_messages,
            provider_tools,
        )

        # 4. 归一化模型响应
        return normalize_response(
            raw_response
        )
```

这是接口伪代码，不是可以直接运行的 Provider SDK 调用。

但是请注意：

`AgentHarness.run()` 不需要改变。

`Dispatcher` 不需要改变。

`ToolRegistry` 也不需要改变。

因为真正的模型与测试替身遵循同一套内部契约：

```text
messages + tool_definitions
              |
              v
          ModelReply
```

这就是 Model Adapter 的价值。

### 为什么这比直接在 Agent Loop 中调用 SDK 好？

如果把某家 Provider 的 SDK 写死在 Agent Loop 里：

```python
client.responses.create(...)
```

未来切换模型时，就必须修改 Loop 的实现。

而现在只需要替换：

```python
model = ScriptedModel(...)
```

为：

```python
model = RealModelAdapter(...)
```

Agent Loop 仍然只关心：

```python
reply.calls
reply.text
```

不过，真实模型还需要正确处理流式事件、工具调用 ID、请求重试和 Token Usage。这些并没有在教学版里实现。

---

## 十三、还有哪些地方尚未达到生产级？

这非常重要。

我们已经做出了一个**可运行的最小 Harness**，但没有做出一个生产级 Coding Agent。

下面明确列出差距。

### 13.1 Execution Environment 还没有真正的 Sandbox

我们的测试工具调用：

```python
subprocess.run(...)
```

会在当前宿主机的操作系统权限下执行。

`cwd` 和路径校验不能阻止测试代码访问工作区之外的资源。

真正面对陌生代码仓库时，需要选择经过加固的容器、虚拟机或操作系统级 Sandbox，并限制网络、文件系统、资源与进程权限。

### 13.2 Approval 还不是完整的 HITL

我们用：

```python
policy.grant(...)
```

模拟可信用户完成审批。

真正的系统还需要：

```text
审批者身份
权限范围
审批界面
持久化请求
过期时间
拒绝处理
操作状态变化检查
审计记录
```

而且审批凭证不能仅依赖内存中的 Hash 集合。

### 13.3 Session 没有持久化

当前：

```python
Session(...)
```

存在内存中。

Python 进程重启后，Pending Tool Calls 和 Message History 都会丢失。

生产级系统需要持久化 Session、Checkpoint 和待审批任务状态。

### 13.4 Context Builder 还没有真正的 Token Budget

当前使用：

```python
len(json.dumps(messages))
```

作为简化的大小检查。

它不是准确的 Token 计算。

也没有实现：

```text
Summarization
Compaction
Artifact Offloading
Memory Retrieval
```

不过保留单独的 ContextBuilder 接口，是为了未来能够增加这些功能而不修改 Agent Loop。

### 13.5 Agent Loop 还没有异步并发和真实取消

目前每个 Tool Call 都是串行处理。

生产系统可能需要：

```text
Parallel Tool Calls
Async Execution
Cancellation Token
Timeout Propagation
Background Subagents
```

特别是执行外部命令时，取消必须能够真正终止进程或执行任务，不能只设置一个 Python 标志。

### 13.6 完成状态还不能证明任务成功

当前：

```python
return "completed"
```

只代表模型没有请求更多工具、当前 Turn 正常结束。

它不能证明退款公式适用于所有情况。

比如，浮点数用于货币计算会引入其他精度问题。正式的财务业务应使用明确的金额表示、舍入规则和 Decimal 等合适机制。

因此真实系统还需要独立 Verification。

### 13.7 Event Recorder 还不是完整的 Tracing

目前只有：

```python
events.events
```

用于内存中的结构化事件。

没有：

```text
Trace ID
Span ID
模型请求耗时
工具输出脱敏
持久化日志
跨 Subagent 关联
```

但所有关键执行路径已经通过 EventRecorder 连接起来，后面可以替换成 OpenTelemetry 等实现。

---

## 十四、现在对照真实开源项目：我们的组件分别对应什么？

我们先不看具体代码片段，而是从架构责任出发。

| 我们的教学实现 | Codex | DeepAgents / LangGraph | OpenCode |
|---|---|---|---|
| `AgentHarness.run()` | `run_turn()` | Graph Agent Loop | Session Execution |
| `ScriptedModel` 接口 | `ModelClient` | `BaseChatModel` | LLM Client / Provider |
| `ContextBuilder` | Prompt / History / Compaction | Context + Middleware | Session Message / Model Transport |
| `ToolRegistry` | `ToolRegistry` | ToolNode + Tool 集合 | `ToolRegistry` |
| `Dispatcher` | Tool Dispatch Runtime | ToolNode + Middleware | Tool `settle()` |
| `Policy` | Approval / ToolOrchestrator | HITL Middleware / 权限规则 | Permission Rules |
| `LocalBackend` | Sandbox / Exec | Backend Protocol | Shell Service |
| `Session.pending` | Turn 运行状态 | Graph State / Interrupt | Session Execution State |
| `EventRecorder` | Telemetry / Rollout | LangSmith / Graph Events | Event Bus / Session Log |

这些是职责映射，而不是一一对应的代码翻译。

我们没有复制其中任何一个项目的具体组织方式。

### 14.1 对照 Codex：为什么 run_turn 不能直接执行全部逻辑？

真实源码：

[Codex run_turn](https://github.com/openai/codex/blob/main/codex-rs/core/src/session/turn.rs)

`run_turn()` 的核心责任是控制一个 Turn 的执行生命周期。

模型采样、工具调用和结束条件被组织在这个执行过程中。

Tool 执行又进一步委托给：

[Codex ToolRegistry](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/registry.rs)

这与我们自己的设计相同：

```text
Agent Loop
    |
    v
Tool Dispatcher
```

为什么要这么做？

因为一个 Turn 可能包含很多不同工具，Loop 不应该因此不断膨胀。

但 Codex 比我们的实现复杂得多，还要处理：

- Pending User Input。
- Compaction。
- Cancellation。
- Stop Hooks。
- Async Tool Tasks。
- Subagent。
- Sandbox Escalation。

因此我们的实现只能理解为核心骨架。

### 14.2 对照 DeepAgents：为什么它不需要自己实现全部循环？

源码：

[DeepAgents graph.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/graph.py)

重点阅读：

```python
create_deep_agent(...)
```

它会组合：

```text
FilesystemMiddleware
SubAgentMiddleware
SummarizationMiddleware
SkillsMiddleware（按配置）
HumanInTheLoopMiddleware（按配置）
```

然后把这些能力装配到 LangChain 的 Agent 执行框架。

DeepAgents 本身不需要从零重新实现我们的所有控制流。

原因是：

**底层 LangChain / LangGraph 已经提供了 Agent Loop、工具执行和状态管理基础设施。**

DeepAgents 主要负责在这套基础上加入更适合复杂任务的能力组合。

如果我们使用 LangChain，就可以把自己的 `ToolRegistry` 和 `Dispatcher` 替换为框架已有的工具执行机制。

但仍然需要理解底层调用链，才能知道 Middleware 什么时候运行、哪些数据会进入模型上下文。

### 14.3 对照 OpenCode：为什么 Session、Tool 和 Permission 需要分离？

源码：

[OpenCode Session](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session.ts)

[OpenCode Tool Registry](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/tool/registry.ts)

[OpenCode Tool Definition](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/tool/tool.ts)

OpenCode 的工具定义将：

```text
Tool Name
Input Schema
Output Schema
Executor
Runtime Context
```

组织成统一的工具契约。

其中 Registry 能生成模型需要的 Tool Definition，并为工具执行提供分发入口。

这与我们在教学代码里把：

```python
tool.definition()
tool.check(...)
tool.execute(...)
```

放在同一个 Tool 对象中，是相似的设计思路。

但 OpenCode 的实现还有更多运行时能力：

- Session 级状态和事件。
- 动态工具注册。
- 权限规则。
- 工具结果存储。
- Provider 适配。
- Plugin Hooks。
- MCP 工具。

我们的简化实现主要是为了看清核心责任，不是替代这些工程能力。

---

## 十五、如果从这个最小 Harness 继续扩展，顺序应该是什么？

现在我们已经有一套可执行骨架。

接下来增加功能时，应该优先解决真实问题，而不是一次性复制成熟框架。

### 阶段一：让模型真正参与决策

替换：

```python
ScriptedModel
```

为：

```python
RealModelAdapter
```

同时正确实现模型消息格式、工具协议和流式输出。

### 阶段二：建立持久 Session

将：

```python
Session.messages
Session.pending
```

保存到持久存储。

实现审批后跨进程恢复，并处理重复提交和已执行工具的幂等性。

### 阶段三：加强执行环境

把直接运行的：

```python
LocalBackend
```

替换为具有明确隔离保证的执行后端。

让文件访问和命令执行都经过受控环境。

### 阶段四：增加真实 Context Manager

引入：

```text
Token Budget
Compaction
Artifact Store
Memory
```

避免 Agent 长时间运行后超出上下文窗口。

### 阶段五：增加 Planning 与 Subagent

对于复杂任务再加入：

```text
Task State
Replanning
Subagent Handoff
Parallel Task Management
```

不要在简单任务里强制增加不必要的规划成本。

### 阶段六：增加 Observability 和 Evaluation

记录：

```text
Model Request
Tool Call
Tool Result
Approval
Context Transformation
Task Verification
```

然后以真实任务完成率，而不是模型输出的“成功”二字，作为最终评估标准。

---

## 十六、回头看：我们已经拥有一套完整的 Agent Harness 心智模型

现在，如果有人问：

> Agent Harness 到底是什么？

我们就可以给出一个比“Agent Loop 加工具”更加准确的理解。

**Agent Harness 是将模型决策组织成连续、受控、可观察、可恢复的真实执行过程的一套运行系统。**

它不是某一个类，也不是某一个大循环。

其中：

```text
Agent Loop
    管理行动循环

Model Adapter
    管理模型 API 差异

Context Builder
    决定模型看见什么

Tool System
    组织和分发行动能力

Permission
    决定是否允许执行

Execution Environment
    在真实环境中执行

Session / Checkpoint
    管理状态与恢复

Planning
    组织目标和进度

Human-in-the-Loop
    处理需要人类参与的控制流

Observability
    记录和解释执行过程
```

它们最终组成：

```text
                  User Goal
                      |
                      v
                    Session
                      |
                      v
                  Agent Loop
                      |
              +-------+-------+
              |               |
              v               v
        Context Builder    Task State
              |
              v
         Model Adapter
              |
              v
            Model
              |
              v
         Model Response
              |
              v
         Tool Dispatcher
              |
      +-------+-------+
      |               |
      v               v
 Tool Registry    Permission
      |               |
      +-------+-------+
              |
              v
      Execution Backend
              |
              v
         Tool Result
              |
              v
         Event Recorder
              |
              v
           Session
              |
              v
          Agent Loop
```

**模型是决策生成者，Harness 是执行组织者。**

这句话贯穿了整个系列。

---

## 十七、最后一个思考题：这个 Harness 真正的核心不变量是什么？

不要把这道题理解成需要记住多少模块名称。

更重要的是理解，一个可靠的 Harness 至少需要保证什么。

我们可以从已经实现的代码中总结：

**不变量一：没有合法工具定义，就不能进入对应工具执行。**

**不变量二：模型的工具参数，必须经过实际运行时验证。**

**不变量三：没有必要授权的操作，不得产生副作用。**

**不变量四：工具请求和工具结果，必须保持正确关联。**

**不变量五：暂停时，尚未完成的操作不能被错误标记为完成。**

**不变量六：工具执行结果必须能够进入下一次模型决策的输入。**

**不变量七：一次模型响应结束，不等于整个任务成功。**

**不变量八：Runtime 事件与模型可见消息应该能被区分和追踪。**

这八条比背诵某个框架的目录结构更加重要。

因为未来无论研究 Codex、DeepAgents、OpenCode，还是自己开发一个新的 Agent Harness，最终都会反复遇到这些问题。

---

## 本章总结

我们从一次最简单的模型调用开始：

```python
response = llm.chat(messages)
```

一步步引入：

```text
模型返回 Tool Call
       |
       v
Agent Loop
       |
       v
Tool Registry
       |
       v
参数验证
       |
       v
Permission
       |
       v
Execution Backend
       |
       v
Tool Result
       |
       v
Session History
       |
       v
Context Builder
       |
       v
下一次模型调用
```

然后加入暂停与恢复、执行事件和测试。

最终，我们得到了一套能够真实读取文件、修改代码、运行测试，并在需要审批时暂停的最小 Harness。

这一章最重要的结果不是那几百行 Python 代码。

而是我们终于能够清楚地回答：

> **一个模型产生的行动意图，是怎样经过 Harness 中各个职责边界，变成真实世界中的一次受控执行，并反馈到模型的下一次决策中的。**

这就是 Agent Harness 的核心运行机制。

## 真实源码阅读路线

建议按照我们自己的执行链逐层阅读。

### Codex：从 Turn 控制流开始

1. [run_turn](https://github.com/openai/codex/blob/main/codex-rs/core/src/session/turn.rs)
2. [Tool Registry](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/registry.rs)
3. [Tool Orchestrator](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/orchestrator.rs)

阅读重点：模型响应如何转换成工具执行，以及完成或继续的判断发生在哪里。

### DeepAgents：从 Middleware 装配开始

1. [create_deep_agent](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/graph.py)
2. [FilesystemMiddleware](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/filesystem.py)
3. [LangChain ToolNode 架构说明](https://github.com/langchain-ai/langchain/blob/master/openwiki/agent-execution.md)

阅读重点：复杂功能怎样作为 Middleware 插入现有 Agent Loop，而不是重新编写循环。

### OpenCode：从 Session 和工具契约开始

1. [Session](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session.ts)
2. [Tool Registry](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/tool/registry.ts)
3. [Tool Definition](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/tool/tool.ts)

阅读重点：工具定义、参数校验、执行入口、权限和 Session 状态怎样协作。

---

# 全系列结束后的学习方向

我们已经完成了 Agent Harness 的核心运行模块。

下一步不必继续无限增加概念，而应该开始做**横向架构对比**：

同一个任务，在 Codex、DeepAgents 和 OpenCode 中分别经过怎样的调用链？

例如：

> 用户发送“修复优惠券退款 Bug”，模型产生第一次 Tool Call，工具执行并返回结果。

分别追踪三个项目中的：

```text
User Input
  → Session
  → Context Assembly
  → Model Request
  → Tool Call
  → Tool Dispatch
  → Execution
  → Tool Result
  → Next Model Request
```

对照它们的真实源码，就能够进一步理解不同 Harness 为什么选择不同的模块边界，以及这些选择各自解决了什么工程问题。
