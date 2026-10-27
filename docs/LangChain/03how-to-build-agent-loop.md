---
title: "02｜一次工具调用结束后，模型为什么还能继续？"
date: 2026-10-27
slug: /langchain-langgraph-source/how-to-build-agent-loop
description: "沿退款测试失败这一条真实执行链，从只运行一次的工具调用走到最小 Agent Loop，再对照 LangChain 的模型消息接口与 LangGraph ToolNode 的结果返回路径，弄清反馈、消息历史和终止判断分别由谁负责。"
tags:
  - LangChain
  - LangGraph
  - Agent
  - Agent Loop
  - 源码精读
  - Python
---

> 本文是「LangChain × LangGraph 源码精读」的**第 02 章**。建议先读[第 01 章：模型提出工具调用，究竟是谁执行了它？](/langchain-langgraph-source/how-to-execute-a-tool-call)。按照博客文件的连续编号，本篇文件名为 `03how-to-build-agent-loop.md`。
>
> **本章只解决一个问题：工具执行结束后，结果怎样返回模型，并让下一次决策真正发生？** 我们会解释一条最小循环，但暂不讨论 Graph 的编译、节点调度或状态合并；那是接下来几章要回答的问题。

上一章已经做到了一件实在的事：模型可以提出 `run_tests(test_refund.py)` 请求，`ToolNode` 可以通过已注册的工具执行真实 `pytest`，然后返回一条包含失败信息的 `ToolMessage`。

可是 `pytest` 返回了 `exit_code=1` 之后，谁来继续修复呢？

不妨想象程序只写了两行：

```python
# 教学化简：假定 node 和 model_message 已在上一章创建。
result = node.invoke({"messages": [model_message]})
print(result["messages"][0].content)
```

这会打印测试结果。但打印结束后，程序也就结束了。**工具确实运行过，不等于 Agent 会继续行动。**

我们此刻缺少的不是新的测试工具，而是一个很小、却决定了 Agent 行为的连接：把工具反馈送进**下一次模型调用**。

<!-- truncate -->

## 1. 先看失败：工具返回了结果，模型却不知道

沿用贯穿案例。`refund.py` 仍然存在浮点数精度问题：

```python
# 教学案例：非生产级金额计算。
def refund_amount(item_price: float, allocated_coupon: float) -> float:
    return item_price - allocated_coupon
```

`test_refund.py`：

```python
from refund import refund_amount


def test_partial_refund_exact_money():
    assert refund_amount(0.3, 0.2) == 0.1
```

上一章的分派逻辑已经能得到类似这样的工具回复：

```python
# 概念数据：不是实际截获的模型响应。
{
    "role": "tool",
    "name": "run_tests",
    "tool_call_id": "call_001",
    "content": "exit_code=1\n...FAILED test_refund.py...",
}
```

如果下一次调用模型时只传入最初的用户要求——“修复退款金额计算”——而不传这条工具回复，那么新的一次模型请求就缺少刚刚发生的事实。它无法仅凭这次输入判断 `pytest` 已经执行、失败在哪儿，甚至可能重复提出同一个工具请求。

一种简单的修复方式，是把这条工具回复放进发送给模型的消息列表。于是我们至少需要保存以下顺序：

```text
用户：修复 refund.py，并运行 pytest
    ↓
模型：请求 run_tests(test_refund.py)，id=call_001
    ↓
工具：回复 exit_code=1，tool_call_id=call_001
    ↓
再次调用模型（携带以上消息）
```

这里有两个不能省略的环节：

- **保留模型之前发出的工具请求。** 工具回复必须能够对应到这次请求。
- **主动再次调用模型。** 消息被保存下来只是数据准备好了，不代表它会自动触发模型运行。

这样，我们才从“一次工具执行”走向“连续决策”。

## 2. 不需要 Graph，先用一个 while 就能闭环

先写下最小结构。这里的 `model` 和 `dispatch` 只是教学接口，暂时不属于任何框架：

```python
# 概念伪代码：用来说明循环控制，不能单独运行。
messages = [user_message]

for _ in range(MAX_ROUNDS):
    ai_message = model(messages)        # 一次明确的模型调用
    messages.append(ai_message)         # 保存本次工具请求或最终回答

    if not ai_message.tool_calls:       # 没有工具请求：退出这段循环
        break

    for call in ai_message.tool_calls:
        tool_message = dispatch(call)    # 实际执行
        messages.append(tool_message)   # 反馈进入下一轮输入
```

请注意：**让循环继续的不是工具，也不是消息类型本身，而是外层控制代码再次调用了 `model(messages)`。**

为什么要先把 `ai_message` 保存下来，再保存 `tool_message`？因为后者回答的是前者提出的请求。若模型一次提出多个工具调用，还应保留每个结果的请求 ID，不能随便将某条失败信息归给另一个调用。

先别急着把它变成 Graph。这个 `for`/`while` 结构对于小任务完全合理；我们需要先确认它的运行行为，再决定框架到底代替了我们做什么。

## 3. 让同一个退款案例真实运行三次工具调用

要验证这段机制，暂时没必要接入需要 API Key 的大模型。我们用一个**脚本化的假模型**：它不是在推理代码，而是读取上一条工具结果，按事先写好的条件决定下一步。

实验目标非常明确：

```text
run_tests → pytest 失败
     ↓
apply_fix → 写入教学用修复代码
     ↓
run_tests → pytest 通过
     ↓
最终回答
```

其中 `pytest` 是真实运行的；“选择修复方案”是人为预设的，所以它**只验证反馈循环，不验证模型具有修复 Bug 的能力**。

创建 `demo_agent_loop.py`，直接运行即可。这个实验会在系统临时目录中生成文件，不会覆盖你实际项目中的 `refund.py`。

```python
"""教学化简：Fake Model + 真实 pytest，不依赖 LangChain，也无需 API Key。"""
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import sys

BROKEN = '''def refund_amount(item_price: float, allocated_coupon: float) -> float:
    return item_price - allocated_coupon
'''
TEST = '''from refund import refund_amount


def test_partial_refund_exact_money():
    assert refund_amount(0.3, 0.2) == 0.1
'''
FIXED = '''from decimal import Decimal


def refund_amount(item_price: float, allocated_coupon: float) -> float:
    return float(Decimal(str(item_price)) - Decimal(str(allocated_coupon)))
'''


def scripted_model(messages: list[dict]) -> dict:
    """依据上一条工具结果选下一步；这只是脚本，不是 LLM。"""
    last = messages[-1]
    if last["role"] == "user":
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_001", "name": "run_tests", "args": {"target": "test_refund.py"}}
        ]}
    if last["role"] != "tool":
        raise ValueError("Expected a tool reply")
    if last["name"] == "run_tests" and "exit_code=1" in last["content"]:
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_002", "name": "apply_fix", "args": {"target": "refund.py"}}
        ]}
    if last["name"] == "apply_fix" and last["content"] == "patch_applied":
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_003", "name": "run_tests", "args": {"target": "test_refund.py"}}
        ]}
    if last["name"] == "run_tests" and "exit_code=0" in last["content"]:
        return {"role": "assistant", "content": "退款精度的这个测试已通过。", "tool_calls": []}
    return {"role": "assistant", "content": "无法确认任务成功。", "tool_calls": []}


def dispatch(call: dict, root: Path) -> dict:
    name, target = call["name"], call["args"]["target"]
    if name == "run_tests" and target == "test_refund.py":
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", target],
            cwd=root, capture_output=True, text=True, timeout=30, check=False,
        )
        content = f"exit_code={completed.returncode}\n{completed.stdout}"
    elif name == "apply_fix" and target == "refund.py":
        (root / "refund.py").write_text(FIXED, encoding="utf-8")
        content = "patch_applied"
    else:
        raise ValueError(f"Not allowed: {name}({target})")
    return {"role": "tool", "name": name, "tool_call_id": call["id"], "content": content}


def main() -> None:
    with TemporaryDirectory() as folder:
        root = Path(folder)
        (root / "refund.py").write_text(BROKEN, encoding="utf-8")
        (root / "test_refund.py").write_text(TEST, encoding="utf-8")
        messages = [{"role": "user", "content": "修复 refund.py 并用 pytest 验证"}]
        tool_replies = []
        for turn in range(1, 6):  # 给循环设置上限；不等于模型智能
            reply = scripted_model(messages)
            messages.append(reply)
            calls = reply["tool_calls"]
            if not calls:
                print(f"round={turn} model_final={reply['content']}")
                break
            for call in calls:
                print(f"round={turn} model_call={call['name']} id={call['id']}")
                tool_reply = dispatch(call, root)
                assert tool_reply["tool_call_id"] == call["id"]
                messages.append(tool_reply)  # 关键：下一轮模型将读到它
                tool_replies.append(tool_reply)
                print(f"          tool_result={tool_reply['content'].splitlines()[0]}")
        else:
            raise RuntimeError("Turn limit exceeded")

        assert [item["name"] for item in tool_replies] == [
            "run_tests", "apply_fix", "run_tests"
        ]
        assert "exit_code=1" in tool_replies[0]["content"]
        assert "FAILED test_refund.py" in tool_replies[0]["content"]
        assert "exit_code=0" in tool_replies[2]["content"]
        assert "1 passed" in tool_replies[2]["content"]
        assert reply["content"] == "退款精度的这个测试已通过。"
        print("verified: failing pytest -> patch -> passing pytest")


if __name__ == "__main__":
    main()
```

执行：

```bash
python -m pip install pytest
python demo_agent_loop.py
```

本地**实际执行**的输出如下（中间的完整 pytest 报告由工具收集，控制台只打印其第一行）：

```text
round=1 model_call=run_tests id=call_001
          tool_result=exit_code=1
round=2 model_call=apply_fix id=call_002
          tool_result=patch_applied
round=3 model_call=run_tests id=call_003
          tool_result=exit_code=0
round=4 model_final=退款精度的这个测试已通过。
verified: failing pytest -> patch -> passing pytest
```

这段运行不只是在字符串里写了“通过”：第一次 `pytest` 的输出经过断言，确实包含 `FAILED test_refund.py`；第三次调用实际再次执行 `pytest`，并断言包含 `1 passed`。两次的进程退出码也分别被断言为 `1` 和 `0`。

### 关键变化究竟是哪一行？

回看完整实验中的这两行：

```python
messages.append(tool_reply)   # 将真实工具结果保存进历史
# 下一轮：scripted_model(messages) 读取刚刚追加的内容
```

**第一行没有让 pytest 再运行一次，它只是建立了下一轮输入。** 之后 `for` 循环重新执行到 `scripted_model(messages)`，才让下一步决策发生。

可以直接做一个反例：删除 `messages.append(tool_reply)`，但保留第二次模型调用。现在假模型读到的最后一条消息仍是上一次的 assistant 工具请求，而不是新的工具结果。这个特定的脚本会报错：

```text
ValueError: Expected a tool reply
```

这个反例也已经在本地实际执行过。它说明，**只有循环而没有反馈，下一次调用不具备依赖上一步结果作决策的必要信息**。真正的大模型未必报同样的异常，但信息缺失的事实不变。

代码里还有一行值得留意：

```python
for turn in range(1, 6):
```

循环不是越久越好。模型可能持续请求同一个失败工具，也可能不断生成新请求，因此调用者需要终止上限、超时和业务验收条件。本例中，假模型在看到测试通过时输出最终回答，并且我们用独立的断言确认实际测试结果；二者不能互相替代。

顺带说明金额计算的边界：本例中的修复代码把十进制结果重新转为 `float`，只是为了保持与上一章 `float` 输入输出示例兼容、通过当前这一条精度测试。**它不构成生产级金钱计算方案**。正式金融金额处理应围绕 `Decimal` 的端到端表示、舍入与币种精度定义单独设计；本系列研究的是执行机制。

## 4. 回到源码：真实模型怎样接收“上一轮的工具反馈”？

最小实验已经让我们看见了需要什么：一个可以读取整个消息列表、返回下一条模型消息的接口。

现在才回到 LangChain Core 的真实实现。

**固定基线：** `langchain-core 1.7.0`，Commit [`f4e6cde98d85910cef5d1f43e26e7a497ae861f3`](https://github.com/langchain-ai/langchain/commit/f4e6cde98d85910cef5d1f43e26e7a497ae861f3)。源码位置：[`libs/core/langchain_core/language_models/chat_models.py` — `BaseChatModel.invoke()`](https://github.com/langchain-ai/langchain/blob/f4e6cde98d85910cef5d1f43e26e7a497ae861f3/libs/core/langchain_core/language_models/chat_models.py)。

在这一版中，`BaseChatModel.invoke()` 接收 `LanguageModelInput`。关键签名的**真实源码节选**是：

```python
def invoke(
    self,
    input: LanguageModelInput,
    config: RunnableConfig | None = None,
    *,
    stop: list[str] | None = None,
    **kwargs: Any,
) -> AIMessage:
```

暂时不用钻进模型厂商适配或缓存逻辑。源码接着会把输入转换为模型能够使用的提示结构；当输入是消息序列时，会经 `_convert_input()` 组织为 `ChatPromptValue`，然后调用 `generate_prompt(...)`，最终取出一条 `AIMessage`。

它在当前问题上的意义是：**调用者可以明确将历史消息作为一次新的模型输入；模型接口本身不是隐藏的无限循环。** 一次 `invoke()` 返回一条模型消息后，是否继续调用仍需要外层程序决定。

对照纯 Python 实验：

| 我们的实验 | LangChain Core 对应角色 | 解决的问题 |
| --- | --- | --- |
| `scripted_model(messages)` | `BaseChatModel.invoke(messages)` | 输入已有消息，取得新的模型回复 |
| `reply["tool_calls"]` | `AIMessage.tool_calls` | 读取模型提出的工具请求 |
| `messages.append(reply)` | 调用者保存 `AIMessage` | 保留请求及其上下文 |

从真实代码到教学代码，输入输出类型不同，**职责对应但实现并不相同**。脚本化假模型不会调用任何模型服务；真实的 `BaseChatModel` 子类需要具体的模型实现。

## 5. 为什么不能只把 pytest 输出作为一条普通用户消息？

如果只是一个一次性玩具程序，甚至可以把失败文本拼接成用户提示再次调用模型。它可能也能得到下一步建议。

但当同一轮模型可以请求多个工具，我们很快会遇到一个问题：**某份结果到底对应哪个请求？**

想象同一次模型响应中包含：

```text
call_001 → run_tests(test_refund.py)
call_002 → read_file(refund.py)
```

两个结果的完成时刻和内容不一定有明显顺序，不能指望模型或程序仅靠文字猜测谁回答了谁。这就是上一章已经出现的 `tool_call_id` 在连续对话中的价值。

在固定版本的 [`AIMessage`](https://github.com/langchain-ai/langchain/blob/f4e6cde98d85910cef5d1f43e26e7a497ae861f3/libs/core/langchain_core/messages/ai.py) 中，真实源码包含：

```python
tool_calls: list[ToolCall] = Field(default_factory=list)
```

同一版本的 [`ToolMessage`](https://github.com/langchain-ai/langchain/blob/f4e6cde98d85910cef5d1f43e26e7a497ae861f3/libs/core/langchain_core/messages/tool.py) 中，有这个字段：

```python
tool_call_id: str
```

前者保存请求，后者引用请求 ID。请注意：`ToolMessage` 的 `content` 可以告诉我们 `pytest` 返回了什么，`tool_call_id` 才负责说明**它是哪一次工具请求的结果**。把这两者放回消息历史，下一次模型调用才拥有可解释的上下文。

我们的实验使用普通 Python 字典表示这两类消息；真实框架中它们是有明确类型的消息对象。当前章节只关心关联关系，不提前讨论消息归并规则。

## 6. ToolNode 已经返回了结果，为什么仍不能自动继续？

回到 LangGraph 的工具执行入口。**固定基线：** `langgraph-prebuilt 1.1.1`，Commit [`5eb60aef6f3837a1fb632a8abc6cf8e8da81785b`](https://github.com/langchain-ai/langgraph/commit/5eb60aef6f3837a1fb632a8abc6cf8e8da81785b)。源码位置：[`libs/prebuilt/langgraph/prebuilt/tool_node.py` — `ToolNode._func()` 与 `_combine_tool_outputs()`](https://github.com/langchain-ai/langgraph/blob/5eb60aef6f3837a1fb632a8abc6cf8e8da81785b/libs/prebuilt/langgraph/prebuilt/tool_node.py)。

上一章我们看见 `_func()` 解析请求并进入工具执行。现在把目光放到返回值。它最终会调用：

```python
return self._combine_tool_outputs(outputs, input_type)
```

在这版源码的正常路径下，如果输入是包含 `messages` 的字典、工具执行得到普通 `ToolMessage`（而不是用于特殊控制流的 `Command`），`_combine_tool_outputs()` 会返回形如下面的内容：

```python
# 教学化简：展示普通字典输入下的正常输出形状，非源码原样全文。
{"messages": [ToolMessage(content="exit_code=1\n...", tool_call_id="call_001")]}
```

这里尤其容易产生误解：`ToolNode` 的返回是**本次节点的输出**，而不是“模型已经看到这条结果”。如果像上一章那样单独调用 `ToolNode.invoke(...)`，调用者还得显式地将返回的消息加入自己的 `messages` 列表，然后自己再次调用模型。

因此，这一章的最小框架对照可以写成下面这样：

```python
# 框架 API 使用的教学化简：展示正常的一次循环路径；
# model_with_tools、tool_node 与 messages 需要按实际应用初始化。
# 本段未执行，不能作为独立可运行脚本。
for _ in range(MAX_ROUNDS):
    ai = model_with_tools.invoke(messages)
    messages.append(ai)

    if not ai.tool_calls:
        break

    update = tool_node.invoke({"messages": messages})
    messages.extend(update["messages"])
```

对比之前的纯 Python 循环，逻辑几乎没变：

- `model_with_tools.invoke(messages)` 返回一次模型消息；工具绑定只让模型能够描述可用工具，**不等于工具已经执行**。
- `tool_node.invoke(...)` 执行工具并生成结果；它**不会替独立运行的这段循环自动调用模型**。
- `messages.extend(...)` 是我们在这里手动连接结果与下一次模型调用的动作；将来交给图运行时管理时，这项工作会落到状态更新与调度流程中。

上面展示的是**单条正常路径**，未涵盖 `Command`、复杂消息结构、多工具并发、异常等情况。更重要的是，例子中由我们手动完成的消息追加，**不等同于真实 Graph Runtime 的状态合并实现**；后续源码章节会专门解释两者的差别。

## 7. 一个容易混淆的停止条件

回到实际执行，代码里有一句：

```python
if not ai.tool_calls:
    break
```

它到底证明了什么？

它只证明：**这一条模型响应没有新的工具请求，所以我们选择退出当前调用循环。** 它既不证明 `refund.py` 改对了，也不证明全部测试已经通过。

我们刚刚运行的实验之所以能输出“测试已通过”，还有另一个条件：真实 `pytest` 曾返回 `exit_code=0`，并且程序断言了 `1 passed`。这两件事必须分开记录。

例如，假模型完全可以不请求工具，直接回答“我已修复”。按照上面的停止条件，循环仍然会结束。但如果没有独立的验证结果，我们不能把这种结束认定为成功。

因此，一个面向 Coding Agent 的外层程序，至少要区分三个概念：

1. **模型停止请求工具**：循环控制上的结束。
2. **工具调用正常返回**：执行了工具函数，但函数内容可能报告测试失败。
3. **业务验收通过**：具体的代码与测试已经满足任务要求。

这一章我们验证了第一个机制如何被触发，也用真实 `pytest` 验证了一个非常有限的第三类条件。没有任何机制能仅凭 `if not ai.tool_calls` 推导出“任务一定完成”。

## 8. 回看这条已经跑通的执行路径

现在我们终于能够画出本系列的第一条完整闭环：

```text
┌──────────────────────────────┐
│ 调用模型，传入已有 messages   │◄─────────────────┐
└──────────────┬───────────────┘                  │
               │ AIMessage                       │
               ▼                                 │
      是否有 tool_calls?                         │
           /       \                             │
         无         有                           │
         │          │                            │
         ▼          ▼                            │
        结束     分派并执行工具                    │
                    │                            │
                    ▼                            │
                ToolMessage                      │
                    │                            │
                    ▼                            │
               加入 messages ────────────────────┘
```

这幅图只表达**我们自己写的循环控制与消息数据流**；它不是 LangGraph 的完整内部运行图。模型是否会生成有用的下一步、外部工具拥有什么权限、失败以后怎样恢复，这些问题目前都没有被这个闭环自动解决。

但本章已经把一个重要边界讲清楚了：

> **模型生成请求，工具执行请求，消息记录结果；让结果成为下一次决策输入、决定何时继续或结束的，是外层的执行控制。**

在小任务里，几十行 Python 就足够实现这种控制，我们不必为了使用框架而使用框架。

自然产生的下一个问题是：如果每个 Agent 都需要手写“模型调用—结果保存—工具分派—再次调用—终止条件”，而且还要统一处理配置、异常、扩展行为，那么这段重复的循环是否值得被封装起来？

**下一章，我们将从已经理解的 `while` 出发，追踪 LangChain `create_agent()` 到底封装了什么，以及它为什么最终返回一个可执行的图。** 直到那时，我们才真正需要进入图构建的源码。

---

## 本章源码索引与验证边界

| 文件及真实入口 | 固定版本 | 本章只证明什么 |
| --- | --- | --- |
| [`BaseChatModel.invoke()`](https://github.com/langchain-ai/langchain/blob/f4e6cde98d85910cef5d1f43e26e7a497ae861f3/libs/core/langchain_core/language_models/chat_models.py) | `langchain-core 1.7.0` / `f4e6cde` | 新一次调用可以接收消息历史并返回 `AIMessage` |
| [`AIMessage`](https://github.com/langchain-ai/langchain/blob/f4e6cde98d85910cef5d1f43e26e7a497ae861f3/libs/core/langchain_core/messages/ai.py) | 同上 | `tool_calls` 记录工具请求 |
| [`ToolMessage`](https://github.com/langchain-ai/langchain/blob/f4e6cde98d85910cef5d1f43e26e7a497ae861f3/libs/core/langchain_core/messages/tool.py) | 同上 | `tool_call_id` 记录结果对应的请求 |
| [`ToolNode._func()` / `_combine_tool_outputs()`](https://github.com/langchain-ai/langgraph/blob/5eb60aef6f3837a1fb632a8abc6cf8e8da81785b/libs/prebuilt/langgraph/prebuilt/tool_node.py) | `langgraph-prebuilt 1.1.1` / `5eb60ae` | 节点执行请求并形成工具消息输出 |
| [`create_agent()`](https://github.com/langchain-ai/langchain/blob/fbb8a35da7023ac0382c3b22f047337861bd3486/libs/langchain_v1/langchain/agents/factory.py) | `langchain 1.4.7` / `fbb8a35` | 下一章的起点，本章尚未展开其内部实现 |

**验证说明：** 本章的 `demo_agent_loop.py` 已在本地实际运行，包含两次真实 `pytest` 进程调用、一次文件修改、执行顺序断言、工具调用 ID 断言和最终结果断言。删除反馈消息后的失败实验也已执行。真实源码节选均来自以上固定 Commit；框架 API 对照示例只做接口与源码核对，当前环境没有安装对应版本的 LangChain / LangGraph，因此**没有执行框架 API 示例，也没有调用真实大模型**。上述版本用于可复核的源码阅读，不等于已验证过整组依赖的安装兼容性。
