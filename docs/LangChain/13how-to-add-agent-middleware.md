---
title: "12｜不改 Agent Loop，怎样统一控制模型调用与工具执行？"
date: 2026-10-27
slug: /langchain-langgraph-source/how-to-add-agent-middleware
description: "承接 refund.py 人工审批案例，从多个工具重复执行前校验的问题出发，以最小 Python 包装器理解 Middleware，再沿固定版本 LangChain create_agent、AgentMiddleware 与 LangGraph ToolNode 源码追踪拦截位置、handler 委托、工具反馈及模型调用包装。"
tags:
  - LangChain
  - LangGraph
  - Agent
  - Middleware
  - ToolNode
  - create_agent
  - 源码精读
  - Python
---

> **LangChain × LangGraph 源码精读 · 第 12 章**  
> 上一篇：[第 11 章｜Agent 修改文件之前，如何暂停并等待人工批准？](./12how-to-interrupt-and-resume-agent.md)  
> **本章唯一主问题：** 当不同工具都需要遵守相同的执行约束时，怎样在**不改 Agent Loop、不复制每个工具的校验逻辑**的情况下，确保请求在真正执行前经过统一检查？

上一篇里，我们让 Coding Agent 在修改 `refund.py` 之前提出审批请求，等人类批准后才继续执行。现在这条执行链已经能完成：

```text
model 提出工具请求
  → tools 真正执行
  → ToolMessage 反馈结果
  → model 再做决定
```

我们也知道，LangChain 的 `create_agent()` 会把这条链组织成可执行的 Graph；LangGraph Runtime 负责调度节点、合并 State、处理暂停和恢复。

接下来出现的问题非常具体：**人类可以批准某一次修改，但如果模型把工具参数改成了 `../outside.py`，我们是否仍然允许执行？**

假设 Agent 的工具现在有两个：

- `run_tests(target)`：运行指定测试文件；
- `apply_fix(target)`：对指定业务文件写入修复代码。

它们各自的业务逻辑都不复杂，却需要遵守共同约束：不能随意访问工作目录外的文件，不能让模型自行决定可写路径，拒绝的请求还应该以可关联的工具结果反馈给模型。

最直觉的做法是把路径校验写进每个工具。这当然可以工作。但当一个 Agent 拥有十几个工具，或者多个 Agent 都复用同样的执行要求时，**同一项策略会散落在十几处代码里**。一处遗漏，就形成不一致的执行边界。

我们真正缺少的，不是再多写一个 `if`，而是**在“工具请求即将被执行”这个公共位置，只写一次检查逻辑**。

这就是这一章引入 *Middleware* 的理由。我们暂时不讨论重试器、缓存、动态提示词和所有生命周期 Hook。先让一个危险请求被拦住，再找到真正的源码入口。

<!-- truncate -->

## 1. 重复检查不是错误，但会制造一个更难发现的失败场景

从上一章的简化工具继续：

```python title="最小工具：教学化简，非 LangChain 源码"
def run_tests(target: str) -> str:
    # 这里假设 pytest 执行代码已经存在
    return execute_pytest(target)


def apply_fix(target: str) -> str:
    return write_refund_patch(target)
```

当模型只使用 `run_tests("test_refund.py")` 和 `apply_fix("refund.py")` 时，我们看不到任何问题。

但下一轮模型给出了这样的请求：

```json
{
  "id": "call_unsafe",
  "name": "apply_fix",
  "args": {"target": "../outside.py"}
}
```

这是一个**格式正确**的工具请求，`target` 也是合法字符串；它并没有因为模型用了工具调用协议就天然变得安全。上一章人类审批了 `refund.py` 的具体修改，并不表示模型拥有任何路径的写权限。

我们可以立刻补校验：

```python title="可行但分散的做法：教学化简"
def run_tests(target: str):
    if target != "test_refund.py":
        return "denied"
    return execute_pytest(target)


def apply_fix(target: str):
    if target != "refund.py":
        return "denied"
    return write_refund_patch(target)
```

对只有两个工具的教学案例，这完全合理。如果长期只有这两个函数，就不必为了使用 Middleware 而引入框架。

可是再增加 `read_file`、`search_files`、`replace_line` 后会发生什么？所有工具都要复制一遍对工作目录和目标路径的检查。更棘手的是：如果一次紧急修复要求禁止所有写操作，是否要逐个修改并重新测试所有工具？

我们希望保持工具函数只描述**它能做什么**，让一个公共包装层判断**这次能不能做**。

有了这个明确需求，才值得设计下一层。

## 2. 不依赖框架，先写出最小的执行包装器

先不要想 `AgentMiddleware`。我们只需要一个接收请求、决定是否继续调用原执行器的函数。

```python title="教学化简：最小工具执行包装器，不是框架源码"
def guarded_execute(request, execute):
    allowed = {
        "run_tests": "test_refund.py",
        "apply_fix": "refund.py",
    }

    name = request["name"]
    target = request["args"]["target"]

    if allowed.get(name) != target:
        return {
            "tool_call_id": request["id"],
            "content": "denied: target not allowed",
        }

    return execute(request)
```

这段代码的关键不是字典白名单，而是参数 `execute`。

- `request` 告诉包装器：本次是什么工具、请求参数是什么、调用 ID 是什么；
- `execute(request)` 表示：**真的进入被包装的执行动作**；
- 如果请求不允许，函数**不调用** `execute`，直接构造拒绝结果；
- 如果请求允许，它调用 `execute` 并返回实际结果。

于是模型的同一个工具调用会经历两种不同的可观察路径：

```text
请求 apply_fix("../outside.py")
          ↓
       检查目标
          ↓ 不允许
    返回 denied 结果
          ↓
   写文件代码从未运行
```

```text
请求 run_tests("test_refund.py")
          ↓
       检查目标
          ↓ 允许
      execute(request)
          ↓
   真正运行 pytest
          ↓
    返回测试退出码
```

**这个设计的核心，是将“能否继续执行”与“执行什么”分开。** 这比“把一切都写进每个 Tool 函数”多了一层委托，但我们已经有了需要它的具体原因。

### 新问题：统一校验之后，如何统一记录？

假设现在还希望记录每个工具调用开始和结束的时间、结果类型。把日志再次塞进所有工具，重复问题又回来了。

一个自然的做法是再加一个包装器：

```python title="教学化简：在原执行器外再包一层"
def audit(request, execute):
    print("before", request["name"])
    result = execute(request)
    print("after", request["name"])
    return result
```

调用关系便成了：

```text
audit
  └── policy
        └── real tool execution
```

注意，这里还没有出现新的 Graph 节点。我们只是在同一个执行动作外套上额外的函数调用。**包装器能决定是否调用下一层，这一点正是后面理解 `wrap_tool_call` 的基础。**

## 3. 用真实 pytest 验证：拒绝请求没有触发执行器

如果只是给出上述推理，我们仍然无法证明路径拦截真的发生在文件修改之前。这次把教学代码做成完整、无需 API Key 的实验。

实验会依次产生四个请求：

1. 非法 `apply_fix("../outside.py")`，预期被拒绝，工具执行器调用次数仍为 0；
2. 合法 `run_tests("test_refund.py")`，真实 `pytest` 失败；
3. 合法 `apply_fix("refund.py")`，修改临时目录下的代码；
4. 再次 `run_tests("test_refund.py")`，真实 `pytest` 通过。

完整代码已按独立脚本实际执行。保存为 `demo_middleware.py`：

```python title="demo_middleware.py（已实际执行；纯 Python 教学实现，不是 LangChain）"
"""Pure Python teaching analogy for tool call middleware; NOT LangChain source."""
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable
import subprocess
import sys

BROKEN = 'def refund_amount():\n    return 0.1 + 0.2\n'
FIXED = 'from decimal import Decimal\n\ndef refund_amount():\n    return Decimal("0.30")\n'
TEST = ('from decimal import Decimal\n'
        'from refund import refund_amount\n\n'
        'def test_partial_refund():\n'
        '    assert refund_amount() == Decimal("0.30")\n')

@dataclass(frozen=True)
class Request:
    name: str
    target: str
    call_id: str

@dataclass(frozen=True)
class Reply:
    content: str
    call_id: str

Handler = Callable[[Request], Reply]
Wrapper = Callable[[Request, Handler], Reply]


def compose(wrappers: list[Wrapper], core: Handler) -> Handler:
    handler = core
    for wrapper in reversed(wrappers):
        inner = handler
        def make_layer(w: Wrapper, h: Handler) -> Handler:
            return lambda req: w(req, h)
        handler = make_layer(wrapper, inner)
    return handler


def run_scenario() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'refund.py').write_text(BROKEN, encoding='utf-8')
        (root / 'test_refund.py').write_text(TEST, encoding='utf-8')
        outside = root.parent / 'do-not-change-demo.txt'
        operations: list[str] = []
        events: list[str] = []

        def execute(req: Request) -> Reply:
            operations.append(f'{req.name}:{req.target}')
            if req.name == 'run_tests':
                result = subprocess.run(
                    [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', req.target],
                    cwd=root, text=True, capture_output=True, timeout=30, check=False,
                )
                return Reply(f'exit_code={result.returncode}', req.call_id)
            if req.name == 'apply_fix':
                (root / req.target).write_text(FIXED, encoding='utf-8')
                return Reply('patch_applied', req.call_id)
            raise ValueError('unknown tool')

        def policy(req: Request, handler: Handler) -> Reply:
            allowed = {'run_tests': 'test_refund.py', 'apply_fix': 'refund.py'}
            if allowed.get(req.name) != req.target:
                return Reply('denied: target not allowed', req.call_id)
            return handler(req)

        def audit(req: Request, handler: Handler) -> Reply:
            events.append(f'before:{req.name}:{req.target}')
            response = handler(req)
            events.append(f'after:{req.name}:{response.content}')
            return response

        dispatch = compose([audit, policy], execute)
        denied = dispatch(Request('apply_fix', '../outside.py', 'call-0'))
        assert denied.content.startswith('denied')
        assert not operations
        assert (root / 'refund.py').read_text(encoding='utf-8') == BROKEN
        assert not outside.exists()

        fail = dispatch(Request('run_tests', 'test_refund.py', 'call-1'))
        assert fail.content == 'exit_code=1'
        patch = dispatch(Request('apply_fix', 'refund.py', 'call-2'))
        assert patch.content == 'patch_applied'
        passed = dispatch(Request('run_tests', 'test_refund.py', 'call-3'))
        assert passed.content == 'exit_code=0'
        assert operations == [
            'run_tests:test_refund.py', 'apply_fix:refund.py', 'run_tests:test_refund.py'
        ]
        assert len(events) == 8
        print('blocked: apply_fix(../outside.py) -> denied; executor_calls=0')
        print('allowed: pytest exit=1 -> patch_applied -> pytest exit=0')
        print('wrapper-order:', events[:2])
        print('execution-count:', len(operations))
        print('all assertions passed')

if __name__ == '__main__':
    run_scenario()
```

运行：

```bash
python -m pip install pytest
python demo_middleware.py
```

**本章已实际获得的输出：**

```text
blocked: apply_fix(../outside.py) -> denied; executor_calls=0
allowed: pytest exit=1 -> patch_applied -> pytest exit=0
wrapper-order: ['before:apply_fix:../outside.py', 'after:apply_fix:denied: target not allowed']
execution-count: 3
all assertions passed
```

这段输出提供了三个证据，而不仅是一句“中间件有用”：

- 非法工具请求被包装器消费，执行器的 `operations` 列表仍为空；
- 两次测试确实调用了本机 `pytest`，不是用字符串模拟测试退出码；
- 被拒绝的请求也有前后日志，说明**审计包装器包住了策略包装器**。

本实验的 `compose()` 只是帮助建立“外层包装器调用内层”的直觉，**不能冒充 LangChain 的链式包装实现**。接下来我们去源码里找真正的请求类型、委托入口和执行位置。

## 4. 真正的 `wrap_tool_call` 接口：谁提供 `handler`？

源码阅读基线继续使用本系列既定版本：

- **LangChain `1.4.7`**，固定 Commit [`fbb8a35da7023ac0382c3b22f047337861bd3486`](https://github.com/langchain-ai/langchain/tree/fbb8a35da7023ac0382c3b22f047337861bd3486)；
- **LangGraph `1.2.17`**，固定 Commit [`4c52e129eb1ab69a3da58b799f67929f77d81149`](https://github.com/langchain-ai/langgraph/tree/4c52e129eb1ab69a3da58b799f67929f77d81149)。

先进入 LangChain 的 [`libs/langchain_v1/langchain/agents/middleware/types.py`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/middleware/types.py)，找到 `AgentMiddleware.wrap_tool_call()`。

**真实源码节选（仅保留接口签名）：**

```python
def wrap_tool_call(
    self,
    request: ToolCallRequest,
    handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
) -> ToolMessage | Command[Any]:
```

与我们刚才的纯 Python 设计一一对应：

| 教学实验 | 固定版本源码中的对应物 | 本次解决的需求 |
| --- | --- | --- |
| `Request` | `ToolCallRequest` | 传入这次工具请求及执行上下文 |
| `execute` 参数 | `handler` 回调 | 让 Middleware 决定是否执行下一层 |
| `Reply` | `ToolMessage` 或 `Command` | 把拦截结果交回 Agent 的执行链 |
| `policy()` | 自定义 `wrap_tool_call()` | 执行前统一检查 |

这里没有要求 Middleware 自己了解 `subprocess` 怎样运行，也没有要求它知道 `refund.py` 应怎样修复。它只关心**这次调用请求能否通过**，并决定是否调用 `handler(request)`。

再往下看真实类型来源。`types.py` 从 **LangGraph** 的 [`prebuilt/tool_node.py`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/prebuilt/langgraph/prebuilt/tool_node.py) 引入 `ToolCallRequest` 和 `ToolCallWrapper`。这是一个重要线索：LangChain 定义面向 Agent 的 Middleware 接口，真正的工具执行位置仍在 LangGraph `ToolNode`。

因此这不是两个相互独立的“工具执行系统”，而是同一条链上不同层次的职责。

### `ToolCallRequest` 中哪些东西来自模型？

继续阅读 `ToolCallRequest` 与 `ToolNode`，就会发现请求里不只有模型产生的工具调用，还包含**执行层附带的信息**。

| 内容 | 来源 | 用途 |
| --- | --- | --- |
| `tool_call["name"]`、`tool_call["args"]`、`tool_call["id"]` | 模型输出经消息协议解析后的工具请求 | 确定执行什么，并关联返回消息 |
| `tool` | 工具注册表 / Runtime | 找到真正可执行的 `BaseTool` |
| `state` | 图运行状态 | 为拦截逻辑提供本轮执行上下文 |
| `runtime` | LangGraph Runtime | 提供运行时上下文和相关资源 |

**模型只能提出要调用哪个工具、使用什么参数；它不能凭一个名字就获得任意 Python 函数执行权限。** 工具注册及可执行对象由应用提供。这一点与第 01 章讨论“工具请求不等于执行工具”完全一致。

## 5. 真正执行工具的地方仍是 `ToolNode`

我们已经知道包装器的接口，但仅凭接口还不足以确认它真的挡在执行器前面。下一步必须追到底层函数。

打开固定版本 LangGraph 的 [`libs/prebuilt/langgraph/prebuilt/tool_node.py`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/prebuilt/langgraph/prebuilt/tool_node.py)，先查 `ToolNode.__init__()`：其中接收 `wrap_tool_call` 和 `awrap_tool_call` 两个可选包装器，并把它们保存为节点的执行配置。

这意味着：**构造 `ToolNode` 时只是登记包装行为，不是在此时执行工具。** 这和前面讲过的图构建与运行边界一致。

继续查 `ToolNode._execute_tool_sync()`。真正的工具执行在这里调用：

**真实源码节选：**

```python
response = tool.invoke(call_args, config)
```

在这之前，`ToolNode` 会确认请求对应已注册工具，注入必要的运行时参数，并准备工具调用参数。包装器所拦截的，最终就是通向这一动作的调用路径。

把上面两段源码与第 01 章的工具调用链合起来，我们得到本章目前足够使用的执行示意：

```text
model 生成 AIMessage.tool_calls
               ↓
Graph Runtime 调度 tools
               ↓
ToolNode 获得 ToolCallRequest
               ↓
      wrap_tool_call(request, handler)
          ├── 拒绝 → 返回 ToolMessage
          └── 放行 → handler(request)
                           ↓
                    ToolNode 执行路径
                           ↓
                     BaseTool.invoke()
                           ↓
                     真实工具副作用
               ↓
返回 ToolMessage / Command
```

这张图故意只展开了当前需要的层次，没有提前讨论工具内部注入字段、异步调度或异常包装的全部实现。

### 为什么拒绝请求也要返回一个 `ToolMessage`？

模型发出工具请求时有一个 `id`。如果 Middleware 决定拦截，模型仍然需要收到对应请求的反馈，例如“目标路径不允许”，才能继续选择下一步，而不是以为工具根本没有被回应。

所以最小拒绝分支应该返回：

```python title="面向真实接口的教学示例；不是库源码"
return ToolMessage(
    content="denied: target not allowed",
    name=request.tool_call["name"],
    tool_call_id=request.tool_call["id"],
)
```

`tool_call_id` 不应凭空创造，也不应误用另一个工具调用的 ID。这一机制同时说明：**安全拒绝是一种有业务意义的执行结果，不一定是 Graph Runtime 的异常。**

不过，这只覆盖我们选定的简单工具路径。真实 `wrap_tool_call` 还允许返回 `Command`，例如工具执行可能需要更新 State。是否用 `Command` 应由实际工具结果语义决定，不能随意把任意字典塞回去。关于 `Command` 更复杂的路由与跨图语义，本系列将在需要它的场景下单独研究。

## 6. 在 `create_agent()` 里，我们到底需要改什么？

现在回到 LangChain 的 [`create_agent()`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py)。此前我们把模型、工具和路由装进 Graph；本章新增一个参数：`middleware`。

**框架 API 示例（非源码原文）：**

```python
from langchain.agents import create_agent
from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage

@wrap_tool_call
def enforce_tool_targets(request, handler):
    allowed = {
        "run_tests": "test_refund.py",
        "apply_fix": "refund.py",
    }
    call = request.tool_call
    if allowed.get(call["name"]) != call["args"].get("target"):
        return ToolMessage(
            content="denied: target not allowed",
            name=call["name"],
            tool_call_id=call["id"],
        )
    return handler(request)

agent = create_agent(
    model=model,                 # 上章使用的模型或脚本化假模型
    tools=[run_tests, apply_fix],
    middleware=[enforce_tool_targets],
)
```

请注意**我们没有改 `run_tests()`、`apply_fix()` 的函数体，也没有修改 `model → tools → model` 的路由结构**。变化仅在 `create_agent()` 的配置里：让工厂在合适的执行边界接上包装逻辑。

真实源码中的入口不只是“把 middleware 存入一个字段”。`create_agent()` 会读取各 Middleware 声明的 Hook，并将不同类型的 Hook 接入对应位置。对 `wrap_tool_call` 来说，工厂最终将包装逻辑交给工具执行层；对下面将出现的 `before_model` 来说，工厂则会参与图节点和边的组织。**这些 Hook 的挂载方式并不相同。**

这也是为什么我们不能笼统地说“Middleware 就是在 Graph 外面套一圈”。

## 7. 为什么有些 Middleware 不是工具包装器？

我们已经解决工具执行前的公共校验。但新的需求很快出现：我们希望在**每次模型调用前**查看消息长度，必要时做一次统计或拦截。

如果继续使用 `wrap_tool_call`，显然不合适：这个 Hook 只有在模型已经提出工具调用、进入工具执行阶段时才有机会运行。若模型根本没有调用工具，它不会触发。

因此，这时才需要区分另一种执行时机：**模型调用边界**。

### 第一次只看 `before_model`

假设只想在每轮模型调用前记录当前消息数量，可以使用：

```python title="框架接口示例：记录每次模型调用前的消息数量"
from langchain.agents.middleware import before_model

@before_model
def note_context_size(state, runtime):
    print("messages before model:", len(state["messages"]))
    return None
```

它不像 `wrap_tool_call` 那样收到 `handler`；它收到的是当前 `state` 和 `runtime`，返回的字典会作为状态更新，返回 `None` 则表示不产生更新。

为什么接口不同？因为我们现在要解决的是一个**模型节点执行前的阶段**，而不是“决定是否调用某个 Tool”。源码会为这类 Hook 组织相应的图节点，正常执行顺序可以理解为：

```text
上轮 ToolMessage 已进入状态
             ↓
       before_model
             ↓
          model
             ↓
       后续路由判断
```

这层能力存在的理由，不是某个术语应该被讲到，而是我们的检查对象已经从**单次工具请求**换成了**下一次模型调用**。

### 现在才有必要区分 `wrap_model_call`

如果仅做日志，`before_model` 足够。可是如果新需求是：调用模型时失败则重试一次，或者依据本轮请求切换具体模型，这件事发生在**真正发起模型请求的瞬间**。执行前日志无法包住模型调用本身。

这时才需要：

```python title="教学化简：展示 wrap_model_call 的关键控制形状"
def wrap_model_call(self, request, handler):
    # 在实际框架中 request 是 ModelRequest
    return handler(request)  # 真正委托下一层执行模型调用
```

它的结构与我们已经理解的 `wrap_tool_call` 相同：既可以委托，也可以短路，还可能在受控条件下重复调用。但它包住的是**模型调用**，不是工具调用。

LangChain 固定版本的 [`AgentMiddleware.wrap_model_call()`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/middleware/types.py) 明确接收 `ModelRequest` 和 `handler`，而 [`factory.py` 中 `_chain_model_call_handlers()`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py) 负责组合多个包装器。

**真实源码节选（省略其他分支）：**

```python
composed_handler = compose_two(handlers[-2], handlers[-1])
for h in reversed(handlers[:-2]):
    composed_handler = compose_two(h, composed_handler)
```

这几行恰好能解释纯 Python 实验中 `audit(policy(real_execute))` 为什么可行：多个包装器嵌套时，**先登记的可以成为外层**，于是它能观察到内层允许执行还是拒绝执行后的结果。

请勿从“可以重试”直接得出“应该对所有操作都重试”。重复请求可能增加模型费用，也可能重复工具副作用。对读写文件、扣款、发邮件等操作的重试，需要在实际执行边界另行设计幂等约束。

### 到这里，我们只需要理解三个位置

| 出现新需求时 | 才选择的 Hook | 理由 |
| --- | --- | --- |
| 工具执行前统一审查参数 | `wrap_tool_call` | 要决定本次工具是否真的执行 |
| 每轮模型调用前读取或更新 State | `before_model` | 要介入图中模型节点前的阶段 |
| 模型调用发生时需要委托、重试或替换请求 | `wrap_model_call` | 要包住真正的模型调用动作 |

`after_model`、`before_agent`、`after_agent` 在真实框架中同样存在；但它们不是当前实验所必需的内容。后面如果需要检查**模型生成的完整工具调用集合**或在 Agent 结束后统一收尾，再引入更自然。

## 8. 一份基于真实 API 的完整框架复现脚本

上面我们只展示了如何配置一个 Wrapper。现在把它放回本系列的 Coding Agent 中，让模型先提出一次**不允许的文件修改**，再提出合法测试请求，最终完成修复。

仍然使用**脚本化假模型**。它只负责输出预定的 `AIMessage.tool_calls`，不代表模型真正学会了修复退款代码；文件修改和 `pytest` 才是真实 Python 操作。

以下代码按固定版本接口编写，支持在安装依赖后复现；**本写作环境未安装 `langchain` / `langgraph`，未实际执行这一段框架代码**。

```python title="demo_langchain_middleware.py（框架 API 复现，尚未在本环境运行）"
"""LangChain middleware demonstration with scripted model; no external API key."""
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import subprocess
import sys

from langchain.agents import create_agent
from langchain.agents.middleware import wrap_tool_call
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

BROKEN = 'def refund_amount():\n    return 0.1 + 0.2\n'
FIXED = 'from decimal import Decimal\n\ndef refund_amount():\n    return Decimal("0.30")\n'
TEST = ('from decimal import Decimal\n'
        'from refund import refund_amount\n\n'
        'def test_partial_refund():\n'
        '    assert refund_amount() == Decimal("0.30")\n')


class ScriptedModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return 'scripted-middleware-demo'

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self  # 只提供模型工具声明适配；不会执行工具

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        last = messages[-1]
        if isinstance(last, HumanMessage):
            answer = self._call('call-0', 'apply_fix', '../outside.py')
        elif isinstance(last, ToolMessage):
            text = str(last.content)
            if 'denied:' in text:
                answer = self._call('call-1', 'run_tests', 'test_refund.py')
            elif 'exit_code=1' in text:
                answer = self._call('call-2', 'apply_fix', 'refund.py')
            elif 'patch_applied' in text:
                answer = self._call('call-3', 'run_tests', 'test_refund.py')
            elif 'exit_code=0' in text:
                answer = AIMessage(content='退款测试已经通过。')
            else:
                raise ValueError(f'Unexpected tool response: {text}')
        else:
            raise ValueError(f'Unexpected message: {type(last).__name__}')
        return ChatResult(generations=[ChatGeneration(message=answer)])

    @staticmethod
    def _call(call_id: str, name: str, target: str) -> AIMessage:
        return AIMessage(
            content='',
            tool_calls=[{'id': call_id, 'name': name, 'args': {'target': target}}],
        )


@wrap_tool_call
def enforce_tool_targets(request, handler):
    allowed = {'run_tests': 'test_refund.py', 'apply_fix': 'refund.py'}
    call = request.tool_call
    if allowed.get(call['name']) != call['args'].get('target'):
        return ToolMessage(
            content='denied: target not allowed',
            name=call['name'],
            tool_call_id=call['id'],
        )
    return handler(request)


def main() -> None:
    with TemporaryDirectory() as folder:
        root = Path(folder)
        (root / 'refund.py').write_text(BROKEN, encoding='utf-8')
        (root / 'test_refund.py').write_text(TEST, encoding='utf-8')
        execution_log: list[str] = []

        @tool
        def run_tests(target: str) -> str:
            """Run the teaching pytest target."""
            execution_log.append(f'run_tests:{target}')
            p = subprocess.run(
                [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', target],
                cwd=root, capture_output=True, text=True, timeout=30, check=False,
            )
            return f'exit_code={p.returncode}\n{p.stdout}'

        @tool
        def apply_fix(target: str) -> str:
            """Apply the pre-defined patch to the teaching file."""
            execution_log.append(f'apply_fix:{target}')
            (root / target).write_text(FIXED, encoding='utf-8')
            return 'patch_applied'

        agent = create_agent(
            model=ScriptedModel(),
            tools=[run_tests, apply_fix],
            middleware=[enforce_tool_targets],
        )
        result = agent.invoke(
            {'messages': [HumanMessage(content='请修复 refund.py 并验证')]},
            config={'recursion_limit': 25},
        )

        replies = [m for m in result['messages'] if isinstance(m, ToolMessage)]
        assert 'denied:' in str(replies[0].content)
        assert replies[0].tool_call_id == 'call-0'
        assert execution_log == [
            'run_tests:test_refund.py', 'apply_fix:refund.py', 'run_tests:test_refund.py'
        ]
        assert 'exit_code=1' in str(replies[1].content)
        assert 'patch_applied' in str(replies[2].content)
        assert 'exit_code=0' in str(replies[3].content)
        assert (root / 'refund.py').read_text(encoding='utf-8') == FIXED
        print('expected: denied → pytest failed → patch → pytest passed')


if __name__ == '__main__':
    main()
```

环境准备（在新的虚拟环境中进行）：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install "langchain==1.4.7" "langgraph==1.2.17" pytest
python demo_langchain_middleware.py
```

**预期轨迹（不是本环境实测日志）：**

```text
model → apply_fix(../outside.py)
wrap_tool_call → denied（apply_fix 函数未运行）
model → run_tests(test_refund.py)
tools → pytest exit_code=1
model → apply_fix(refund.py)
tools → patch_applied
model → run_tests(test_refund.py)
tools → pytest exit_code=0
model → 返回最终回答
```

这个演示刻意把“非法路径”放在第一轮。否则 Middleware 永远只放行请求，读者只能看到它被注册，看不到它**究竟拦截了什么**。

请注意，本框架示例为了凸显 Middleware 的职责，将两个 Tool 本身写得很简单。在真实部署中，**不能只依赖这一层 Middleware 充当唯一安全边界**：工具本身、执行环境和身份权限仍需执行最小权限原则。我们不应把教学例子误当成生产沙箱。

## 9. 为什么 Middleware 不能替代上一章的人工审批？

现在已经有了统一的路径检查。有人可能会问：既然 Middleware 能拦截危险操作，是不是不再需要 `interrupt()`？

不行，因为它们回答的问题不同。

本章的路径检查可以在**无需人类参与**的情况下得出确定结论：`apply_fix("../outside.py")` 不在允许目标内，所以立即拒绝。

但上一章的情况是：`apply_fix("refund.py")` **本身是允许的操作类别**，只是在当前业务环境中，需要人工审核这一次的具体修改内容。这不是路径白名单能解决的问题。

| 机制 | 解决的问题 | 不解决的问题 |
| --- | --- | --- |
| Tool 内的业务校验 | 某个工具自身如何安全处理输入 | 所有工具的公共执行策略自动复用 |
| `wrap_tool_call` | 统一检查、变换、观察某一次工具执行 | 自动得到人类授权、自动提供沙箱 |
| `interrupt()` + `Command(resume=...)` | 暂停执行并等待外部决定 | 身份认证、外部文件锁定、精确一次副作用 |
| Checkpoint | 保留图进度，支持恢复 | 自动回滚已经发生的外部修改 |

真实 LangChain 还提供 `HumanInTheLoopMiddleware`，其中的实现会根据工具调用匹配审批策略，并使用 `interrupt()` 等执行机制取得决定。它恰好说明这些概念不是互相竞争的替代品，而是在不同职责层上**组合**。

本章不继续追踪这个预置 Middleware 的所有审批分支——上一章已经解释了动态中断。现在只需要知道：**Middleware 可以选择一个合适的介入位置，而等待外部决定仍需要 Runtime 的中断/恢复机制。**

## 10. 一个容易忽略的边界：Hooks 不是“万能横切基础设施”

理解 Middleware 后，最容易出现的是架构误判：把所有通用能力都塞进 Middleware。

但我们此前的源码已经展示过几个独立职责：

- **State/Reducer** 负责字段写入与合并规则，不因为注册 Middleware 就发生变化；
- **Checkpoint** 负责保存并恢复图执行进度，不因为 `wrap_tool_call` 的存在就具备事务能力；
- **Tool/Backend** 负责真正读写文件或调用外部服务，Middleware 本身并不提供隔离环境；
- **Graph Runtime** 负责节点调度与后继任务的选择，Middleware 不会自动替换整个调度引擎。

另外，包装器可以通过不调用 `handler` 拦截请求，但这也带来新的责任：你必须提供**正确的工具反馈**。如果伪造成功结果，模型可能以为文件已经改好；如果重复调用真实写工具，可能引入重复副作用。Middleware 能改变执行行为，**因此比纯日志 Hook 更需要严格测试**。

对于路径策略，本章为了可复现只使用两个文件名的精确白名单。一般化的路径策略必须考虑符号链接、路径规范化、工作目录变化、竞态条件、权限边界和进程隔离。简单的字符串检查、`startswith` 或“当前工作目录就是沙箱”的假设都不足以建立可信的文件访问安全边界。

## 11. 把新机制放回整条执行主线

我们不需要重画一张巨大的框架架构图。只在已经理解的路径上增加这一层即可：

```text
create_agent() 构建并编译执行图
              ↓
Graph Runtime 运行 model 节点
              ↓
      模型返回工具请求
              ↓
Graph Runtime 调度 tools 节点
              ↓
     ToolNode 收到工具请求
              ↓
      wrap_tool_call
       /          \
    拒绝          放行
     |             |
 ToolMessage    handler(request)
     |             ↓
     |        BaseTool.invoke()
     |             ↓
     |          ToolMessage
     └──────┬──────┘
            ↓
       写入消息状态
            ↓
    下一轮 model 节点
```

这条链只比上一章多了一个清楚的执行边界：**真正执行之前可以进行统一检查，并决定是否委托给下一层。**

我们并没有因此发明新的 Agent Loop，也没有改变 Runtime 的基本职责；模型的工具请求仍然需要与工具结果关联，状态更新仍然由 Graph 处理，下一轮模型调用仍由图执行引擎驱动。

当我们需要统一控制**模型调用**时，类似的包装思想应用到 `wrap_model_call`；当我们需要在模型节点前更新 State 时，使用 `before_model`。这三者不是需要死记的术语表，而是从三个不同的执行位置自然长出来的机制。

## 12. 本章读完，你应该能解释什么？

现在把从头到尾的新设计压缩成一条因果链：

```text
工具各自能工作
   ↓
多个工具需要同一校验规则
   ↓
复制校验容易遗漏或不一致
   ↓
将“是否执行”放在公共委托边界
   ↓
wrapper(request, handler)
   ↓
拒绝：不调用 handler，返回 ToolMessage
放行：调用 handler，进入真正工具执行
   ↓
LangChain create_agent 组织 Middleware
   ↓
LangGraph ToolNode 执行工具
   ↓
状态反馈给下一轮 model
```

本章最重要的不是记住多少个 Hook，而是能亲自回答：

1. 为什么要在**工具请求**与**真实执行**之间增加一层委托？
2. 什么时候可以通过“不调用 handler”阻止一次副作用？
3. 为什么拒绝时仍要带着 `tool_call_id` 返回结果？
4. `before_model` 与 `wrap_model_call` 为什么不能简单视为同一个执行位置？
5. 为什么统一校验不能替代人工审批、Checkpoint 和运行环境隔离？

如果你能沿着 `create_agent → ToolNode → wrap_tool_call → handler → BaseTool.invoke → ToolMessage` 清楚说出每一层的输入、动作和输出，就真正理解了本章的核心机制。

## 13. 下一章：回到这一次完整执行

至此，这套 Coding Agent 的最小执行路径已经长成了一个具有明确职责的系统：模型做决策，工具产生现实副作用，Graph Runtime 负责循环和状态提交，Checkpoint 保存进度，中断机制等待人的决定，Middleware 在需要的调用边界统一增强行为。

但我们一直用不同章节、不同切面观察同一条链。现在有必要做一次**从入口到返回的完整源码走读**：

> 当调用 `create_agent(...).invoke(...)` 时，LangChain 怎样组装模型、工具与 Middleware？LangGraph 又怎样完成一次从 `model` 到 `tools`、提交状态、再回到 `model` 的真实执行？

下一篇：[**第 13 章｜从 create_agent() 到 Runtime：完整追踪一次 Coding Agent 执行**](./14how-to-trace-agent-execution.md)  
建议文件名：`14how-to-trace-agent-execution.md`。

---

## 源码索引与验证边界

**固定版本：**LangChain `1.4.7`（Commit `fbb8a35da7023ac0382c3b22f047337861bd3486`），LangGraph `1.2.17`（Commit `4c52e129eb1ab69a3da58b799f67929f77d81149`）。这是全文源码讨论基线；具体依赖解析与集成兼容性仍需以实际安装结果复核。

| 仓库与源码文件 | 本章关注位置 | 要回答的问题 |
| --- | --- | --- |
| [LangChain `agents/middleware/types.py`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/middleware/types.py) | `AgentMiddleware.wrap_tool_call()` / `wrap_model_call()`、`ModelRequest` | Middleware 接受什么请求？能返回什么结果？ |
| [LangChain `agents/factory.py`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py) | `create_agent()`、`_chain_model_call_handlers()`、图构建入口 | 工厂如何组织 Middleware 与模型、工具执行路径？ |
| [LangGraph `prebuilt/tool_node.py`](https://github.com/langchain-ai/langgraph/blob/4c52e129eb1ab69a3da58b799f67929f77d81149/libs/prebuilt/langgraph/prebuilt/tool_node.py) | `ToolCallRequest`、`ToolNode.__init__()`、`_execute_tool_sync()` | 包装器位于真正的 `tool.invoke()` 之前哪里？ |
| [LangChain `agents/middleware/human_in_the_loop.py`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py) | `HumanInTheLoopMiddleware` 的工具调用审批路径 | Middleware 怎样与上一章的动态中断机制组合？ |

**已经实际运行并验证：**`demo_middleware.py` 纯 Python 实验。它在临时目录里执行真实 `pytest`，获得失败退出码 1 和通过退出码 0；校验非法写请求没有进入工具执行器；所有断言通过。此实验只验证包装器**设计行为**，不验证框架内部实现。

**尚未执行：**`demo_langchain_middleware.py` 的完整 LangChain / LangGraph API 路径。它按固定源码的接口编写，已进行 Python 语法检查，但当前环境没有安装对应框架依赖，所以不能把预期日志表述为框架实测结果，也没有验证真实模型行为。

**教学化简与源码区分：**`guarded_execute()`、`compose()` 以及纯 Python `Request`/`Reply` 均为教学代码；标注“真实源码节选”的少量签名/调用语句来自固定 Commit。其它结构图是为了说明因果关系而绘制的执行示意，不是源代码的逐行复制。
