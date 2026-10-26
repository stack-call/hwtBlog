---
title: 第一章：为什么需要 Agent Loop？
description: 从一次模型调用与固定工作流的局限出发，推导 Agent Loop 的设计原理，使用纯 Python 实现并验证一个最小 Coding Agent，最后对应到 DeepAgents 与 LangChain 源码
tags: [DeepAgents, Agent Loop, Agent Harness, ReAct, 源码精读]
sidebar_position: 1
---

# 第一章：为什么需要 Agent Loop？

## 一、本章要解决的问题

我们从一个具体任务开始：

> 修复电商系统中优惠券部分退款的金额精度问题，修改 `refund.py`，运行测试，并确保退款金额分摊正确。

假设项目中有：

```text
shop/
├── refund.py
└── test_refund.py
```

其中 `refund.py` 使用浮点数进行金额计算。

一个可能的实现是：

```python
def allocate_coupon(total, count):
    return [round(float(total) / count, 2)] * count
```

假设有一分钱优惠券，需要平均分给两个商品。

那么：

```python
allocate_coupon("0.01", 2)
```

这段代码可能得到：

```python
[0.01, 0.01]
```

两项相加：

```text
0.01 + 0.01 = 0.02
```

但原始优惠金额只有：

```text
0.01
```

这意味着金额分摊不满足一个基本约束：

**分摊后的金额之和必须等于原始金额。**

我们要研究的不是具体退款算法，而是：

**怎样构建一个系统，让模型能够发现问题、采取修复行动、运行测试，并根据测试结果继续工作？**

这个问题将自然引出 Agent Loop。

---

## 二、第一种尝试：直接调用一次 LLM

假设我们已经拥有一个模型：

```python
response = model.invoke(
    "请修复退款精度问题"
)
```

模型可能返回：

```text
建议使用 Decimal 代替 float，
避免浮点数精度问题。
```

这个回答也许有帮助。

但是，用户要求的是：

```text
修改代码
运行测试
确保修复成功
```

模型实际完成的只是：

```text
给出建议
```

它没有：

```text
读取 refund.py
修改 refund.py
执行测试
观察执行结果
```

### 2.1 为什么一次模型调用不能直接完成这些操作？

先区分两种行为。

**生成文本：**

```python
model.invoke(...)
```

模型根据输入产生输出。

**改变外部环境：**

```python
write_file(...)
```

```python
run_tests(...)
```

这些操作需要真正的程序执行。

仅仅生成：

```text
我已经修改 refund.py。
```

并不会改变磁盘上的文件。

因此，要让模型完成任务，我们首先需要让它能够调用外部工具。

例如：

```python
read_file(...)
edit_file(...)
run_tests(...)
```

但有了工具以后，是否就足够了？

还不够。

---

## 三、第二种尝试：预先写死整个执行过程

既然任务需要读取、修改和测试，我们可以自己写一个工作流。

```python
source = read_file("refund.py")

new_source = model.invoke(
    f"修复以下代码：\n{source}"
)

edit_file(
    "refund.py",
    new_source.content,
)

result = run_tests()

print(result)
```

这个程序比单次模型调用前进了一步。

因为它真的执行了：

```text
读取
修改
测试
```

但现在考虑一个新的情况。

模型第一次修改后，测试失败：

```text
FAILED test_coupon_one_cent_two_items

Expected total: 0.01
Actual total:   0.02
```

程序执行到：

```python
print(result)
```

就结束了。

它并没有继续修复。

### 3.1 为什么失败之后还需要一次模型调用？

因为第一次修复只是一个假设。

例如模型可能认为：

> 只要把 float 替换成 Decimal，就能解决退款精度问题。

但测试结果告诉我们：

> 即使使用 Decimal，如果每个商品分别四舍五入，也可能导致分摊总额不一致。

因此，模型需要看到新的证据：

```text
FAILED

Expected total: 0.01
Actual total:   0.02
```

再根据证据改变方案。

例如：

```text
不要分别独立四舍五入。

先以整数分计算总金额，再分配余数。
```

这就是任务执行过程中的**反馈**。

### 3.2 反馈带来了什么变化？

原本的程序是：

```text
输入
 |
 v
模型生成修改
 |
 v
执行测试
 |
 v
结束
```

现在我们希望：

```text
输入
 |
 v
模型生成修改
 |
 v
执行测试
 |
 v
测试是否通过？
 |
 +-- 是 --> 结束
 |
 +-- 否 --> 将测试结果反馈给模型
                       |
                       v
                    重新修复
                       |
                       v
                    再次测试
```

从这里开始，程序不再是一条固定的执行链。

它需要根据执行过程中的新信息，决定下一步行动。

---

## 四、固定 Workflow 和 Agent Loop 的真正区别

我们现在可以对比两种执行方式。

### 4.1 固定 Workflow

固定 Workflow 的执行规则主要由开发者预先定义。

例如：

```text
读取文件
    |
    v
修改代码
    |
    v
运行测试
    |
    v
成功？ ----- 否 -----> 再次修改
    |
    是
    |
    v
结束
```

注意：

**固定 Workflow 也可以有条件分支和循环。**

因此，不能简单地认为：

```text
有循环 = Agent
```

这是一个常见误解。

真正的区别在于：

**下一步行动主要由谁决定？**

在固定 Workflow 中，开发者通常预先定义：

```python
if tests_failed:
    retry_patch()
```

程序知道：

```text
测试失败后，只能进入预设的 retry_patch 步骤。
```

但在复杂代码修复任务中，失败以后可能需要：

```text
重新读取源码
读取另一个文件
搜索相关调用
分析测试断言
撤销错误修改
更换修复方案
```

这些步骤不一定能够提前确定。

### 4.2 Agent Loop

Agent Loop 将选择下一步行动的工作交给模型。

```text
                   当前任务信息
                        |
                        v
                       模型
                        |
                        v
                 决定下一步行动
                        |
              +---------+---------+
              |         |         |
              v         v         v
           read_file  edit_file  run_tests
              |         |         |
              +---------+---------+
                        |
                        v
                    执行结果
                        |
                        v
                   反馈给模型
                        |
                        v
                    再次决策
```

模型不需要提前知道所有具体步骤。

它可以根据新获得的证据选择下一步。

这里的关键变化是：

**程序不再只执行预先写死的行动序列，而是反复请求模型决定下一步行动。**

但也需要明确：

模型不是完全不受约束的。

它只能在系统提供的工具和执行规则范围内选择行动。

### 4.3 从架构角度总结

| 维度 | 固定 Workflow | Agent Loop |
|---|---|---|
| 下一步行动 | 主要由开发者定义 | 模型在运行时选择 |
| 执行路径 | 相对明确 | 随观察结果变化 |
| 工具调用顺序 | 通常事先编排 | 可以动态选择 |
| 适合场景 | 稳定、规则明确的流程 | 探索性、需要持续判断的任务 |
| 主要风险 | 难覆盖未预料情况 | 决策不确定，可能重复或错误操作 |

两者不是对立关系。

真实 Agent 系统通常会将确定性 Workflow 与模型驱动的行动选择结合使用。

---

## 五、第三种设计：让模型决定下一步，再执行它的决定

现在，我们已经知道需要：

```text
模型决策
    |
    v
执行操作
    |
    v
观察结果
    |
    v
重新决策
```

这就是一个最小 Agent Loop 的雏形。

但要把它写成代码，首先需要解决：

> 模型怎样表达“下一步要调用哪个工具”？

假设模型回答：

```text
现在需要读取 refund.py。
```

程序可以尝试解析这句话。

但自然语言解析容易存在歧义。

例如：

```text
读取一下退款文件。
```

到底是：

```python
read_file("refund.py")
```

还是：

```python
grep("refund")
```

我们需要一种更清晰的结构。

因此引入：

**Tool Call：结构化的工具调用请求。**

例如：

```python
{
    "name": "read_file",
    "args": {
        "file_path": "refund.py"
    },
    "id": "call-001"
}
```

它表达了：

- `name`：使用哪个工具。
- `args`：给工具什么参数。
- `id`：标识这次调用。

此时模型输出的不只是文本。

它还可以输出一个明确的行动请求。

但这里必须区分：

```text
模型提出 Tool Call
```

和：

```text
工具真正执行完成
```

这两个不同阶段。

Tool Call 只是请求。

真正执行操作的是 Agent 外部的工具执行系统。

---

## 六、Agent Loop 的四个必要组成部分

现在我们可以总结，一个最小 Tool Calling Agent Loop 至少需要什么。

### 6.1 决策者：Model

模型接收当前任务信息，并输出下一步决定。

例如：

```python
model(messages)
```

可能返回：

```python
{
    "name": "read_file",
    "args": {
        "file_path": "refund.py"
    }
}
```

也可能返回：

```text
任务已经完成。
```

### 6.2 行动能力：Tools

工具负责实际操作环境。

例如：

```python
read_file(...)
edit_file(...)
run_tests(...)
```

模型不直接操作操作系统。

它提出请求，由程序执行相应工具。

### 6.3 信息记录：Messages

如果模型第二次调用时看不到第一次的工具结果，就无法利用反馈。

例如：

```text
第一次：

模型调用 read_file
工具返回源码

第二次：

模型必须看到刚才读取的源码
```

因此，最小循环需要记录消息历史。

这里暂时用普通 Python 列表表示：

```python
messages = []
```

后面研究 LangGraph 时，再讨论真正的 State 管理。

### 6.4 运行控制：Loop Controller

最后，还需要一个控制程序负责：

```text
何时调用模型
何时执行工具
何时将结果加入历史
何时结束循环
```

它才是将所有组成部分连接起来的关键。

所以：

```text
                Loop Controller
                       |
                       v
                     Model
                       |
                       v
                下一步行动请求
                       |
             +---------+---------+
             |                   |
             v                   v
          Final Answer        Tool Call
             |                   |
             v                   v
            END               Tool Executor
                                 |
                                 v
                              Observation
                                 |
                                 v
                              Messages
                                 |
                                 v
                                Model
```

这就是我们从实际需求中推导出来的最小结构。

---

## 七、为什么说 Agent Loop 是反馈驱动的？

我们可以用一个更明确的方式描述这个过程。

假设：

```text
Hₜ = 到第 t 轮为止的历史信息
```

模型根据历史决定行动：

```text
aₜ = Model(Hₜ)
```

如果模型决定调用工具：

```text
oₜ = Tool(aₜ)
```

其中：

```text
aₜ = Action，行动
oₜ = Observation，观察结果
```

然后更新历史：

```text
Hₜ₊₁ = Hₜ + aₜ + oₜ
```

再调用模型：

```text
aₜ₊₁ = Model(Hₜ₊₁)
```

这不是在要求模型必须显式生成数学公式。

公式只是在表达：

**下一轮模型决策必须能够依赖上一轮的执行结果。**

### 7.1 它与 ReAct 有什么关系？

ReAct 是一种将推理和行动交替结合的 Agent 研究方法。

典型过程可以表示为：

```text
Reason
   |
   v
Act
   |
   v
Observe
   |
   v
Reason Again
```

也就是：

```text
分析问题
    |
    v
采取行动
    |
    v
观察行动结果
    |
    v
重新分析
```

我们当前构建的 Tool Calling Agent Loop，与这一思想有直接联系。

不过，现代模型不一定将内部推理显式输出为文本。

所以我们不应该将 Agent Loop 严格定义为：

```text
Thought 文本
    |
    v
Action 文本
    |
    v
Observation 文本
```

更稳妥的定义是：

**模型产生行动决策，执行系统产生观察反馈，模型基于新反馈继续决策。**

参考：

[ReAct: Synergizing Reasoning and Acting in Language Models](https://arxiv.org/abs/2210.03629)

---

## 八、现在可以实现最小 Agent Loop 了

经过前面的推导，我们已经有：

```text
Model
Tools
Messages
Loop Controller
```

因此可以写出一个最小实现。

以下是概念伪代码：

```python
messages = [
    {"role": "user", "content": task}
]

while True:

    response = model(messages)

    messages.append(response)

    if not response["tool_calls"]:
        break

    for call in response["tool_calls"]:

        tool = tools[call["name"]]

        result = tool(**call["args"])

        messages.append({
            "role": "tool",
            "name": call["name"],
            "tool_call_id": call["id"],
            "content": result,
        })
```

这里每一部分，都有明确的设计原因。

```python
response = model(messages)
```

让模型根据历史信息决定下一步。

```python
messages.append(response)
```

记录模型刚才的行动请求。

```python
if not response["tool_calls"]:
    break
```

当模型不再请求工具时，结束循环。

```python
tool = tools[call["name"]]
```

根据工具名称找到实际执行函数。

```python
result = tool(**call["args"])
```

真正执行操作。

```python
messages.append({
    "role": "tool",
    ...
})
```

记录执行结果，让下一轮模型能够看到。

### 这个实现还有什么问题？

它没有：

```text
工具权限检查
参数 Schema 校验
真正的状态持久化
异常恢复
Middleware
并发工具调度
人工审批
上下文压缩
```

但这些都属于后续的增强机制。

此时我们应该优先确认：

**最小 Loop 是否已经具备连续决策与反馈能力？**

接下来通过实验回答。

---

# 九、完整实验：从读取文件到测试失败，再到再次修复

这是本章最重要的实验。

我们将创建一个极简 Coding Agent。

它需要：

1. 读取 `refund.py`。
2. 第一次修改代码。
3. 运行真实的 Python 单元测试。
4. 发现第一次修复失败。
5. 再次修改代码。
6. 重新运行测试。
7. 测试通过后结束。

为了把实验聚焦在 Agent Loop 上，我们暂时不接入真实 LLM。

使用一个由 Python 编写的 `fake_model()`，模拟模型根据工具结果做出的决策。

这样可以：

- 不需要 API Key。
- 不需要安装 LangChain。
- 直接观察 Loop 本身。
- 明确区分模型决策、工具执行与循环控制。

需要特别说明：`fake_model()` 的决策规则是预设的，它并不具备真实模型的代码理解能力。实验验证的是**反馈循环机制**，不是模型的智能程度。

## 9.1 实验环境

只需要 Python 3.10+。

创建文件：

```text
agent_loop_demo.py
```

运行：

```bash
python agent_loop_demo.py
```

实验使用 Python 标准库，并在临时目录创建演示文件，不修改当前真实项目。

## 9.2 完整代码

```python
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import sys


# ==========================================
# 1. 模拟项目初始代码
# ==========================================

INITIAL = '''def allocate_coupon(total, count):
    return [round(float(total) / count, 2)] * count
'''


# ==========================================
# 2. 第一次修复：改用 Decimal
#
# 但仍然每项单独四舍五入，
# 可能破坏总额守恒。
# ==========================================

FIRST_PATCH = '''from decimal import Decimal, ROUND_HALF_UP

def allocate_coupon(total, count):
    part = (Decimal(str(total)) / count).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return [part] * count
'''


# ==========================================
# 3. 第二次修复：以整数分分配余数
#
# 这是一个简化的按份数均分演示。
# 实际优惠券分摊通常还需要考虑商品权重、
# 负数、上限以及业务舍入规则。
# ==========================================

SECOND_PATCH = '''from decimal import Decimal

def allocate_coupon(total, count):
    cents = int(
        (Decimal(str(total)) * 100).to_integral_exact()
    )
    base, remainder = divmod(cents, count)
    return [
        Decimal(base + (i < remainder)) / 100
        for i in range(count)
    ]
'''


# ==========================================
# 4. 真实单元测试
# ==========================================

TESTS = '''import unittest
from decimal import Decimal
from refund import allocate_coupon

class RefundTests(unittest.TestCase):

    def test_coupon_one_cent_two_items(self):
        shares = allocate_coupon("0.01", 2)

        self.assertEqual(
            sum(shares),
            Decimal("0.01"),
        )

        self.assertTrue(
            all(
                v == v.quantize(Decimal("0.01"))
                for v in shares
            )
        )

    def test_coupon_three_cents_two_items(self):
        shares = allocate_coupon("0.03", 2)

        self.assertEqual(
            sum(shares),
            Decimal("0.03"),
        )

        self.assertEqual(len(shares), 2)
'''


with TemporaryDirectory() as folder:

    root = Path(folder)

    (root / "refund.py").write_text(
        INITIAL,
        encoding="utf-8",
    )

    (root / "test_refund.py").write_text(
        TESTS,
        encoding="utf-8",
    )

    # ======================================
    # 5. 定义工具
    # ======================================

    def read_file(file_path: str) -> str:
        return (root / file_path).read_text(
            encoding="utf-8"
        )

    def edit_file(
        file_path: str,
        content: str,
    ) -> str:

        (root / file_path).write_text(
            content,
            encoding="utf-8",
        )

        return f"UPDATED {file_path}"

    def run_tests() -> str:

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(root),
                "-p",
                "test_refund.py",
                "-v",
            ],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
            check=False,
        )

        status = (
            "PASS"
            if completed.returncode == 0
            else "FAIL"
        )

        return (
            f"{status} "
            f"(exit={completed.returncode})\n"
            f"{completed.stdout[-700:]}"
        )

    # ======================================
    # 6. 工具注册表
    # ======================================

    tools = {
        "read_file": read_file,
        "edit_file": edit_file,
        "run_tests": run_tests,
    }

    # ======================================
    # 7. 构造工具调用请求
    # ======================================

    def tool_request(
        name: str,
        args: dict,
        messages: list,
    ) -> dict:

        return {
            "role": "assistant",
            "content": f"请求工具：{name}",
            "tool_calls": [
                {
                    "id": f"call-{len(messages)}",
                    "name": name,
                    "args": args,
                }
            ],
        }

    # ======================================
    # 8. 使用规则模拟模型决策
    # ======================================

    def fake_model(messages: list) -> dict:

        last = messages[-1]

        # 首次收到用户任务：
        # 决定先读取代码。
        if last["role"] == "user":

            return tool_request(
                "read_file",
                {"file_path": "refund.py"},
                messages,
            )

        # 读取代码以后：
        # 生成第一次修复方案。
        if (
            last["role"] == "tool"
            and last["name"] == "read_file"
        ):

            return tool_request(
                "edit_file",
                {
                    "file_path": "refund.py",
                    "content": FIRST_PATCH,
                },
                messages,
            )

        # 修改代码以后：
        # 决定执行测试。
        if (
            last["role"] == "tool"
            and last["name"] == "edit_file"
        ):

            return tool_request(
                "run_tests",
                {},
                messages,
            )

        # 根据测试结果决定下一步。
        if (
            last["role"] == "tool"
            and last["name"] == "run_tests"
        ):

            if last["content"].startswith("FAIL"):

                return tool_request(
                    "edit_file",
                    {
                        "file_path": "refund.py",
                        "content": SECOND_PATCH,
                    },
                    messages,
                )

            if last["content"].startswith("PASS"):

                return {
                    "role": "assistant",
                    "content": "修复完成，测试通过。",
                    "tool_calls": [],
                }

        return {
            "role": "assistant",
            "content": "无法继续，需要人工检查。",
            "tool_calls": [],
        }

    # ======================================
    # 9. Agent Loop
    # ======================================

    messages = [
        {
            "role": "user",
            "content": (
                "修复退款精度 Bug，并运行测试。"
            ),
        }
    ]

    for _ in range(12):

        # 第一步：模型决策
        reply = fake_model(messages)

        messages.append(reply)

        # 第二步：判断是否结束
        if not reply["tool_calls"]:

            print("AGENT:", reply["content"])

            break

        # 第三步：执行模型请求的工具
        for call in reply["tool_calls"]:

            name = call["name"]

            result = tools[name](
                **call["args"]
            )

            # 第四步：记录工具执行结果
            messages.append({
                "role": "tool",
                "name": name,
                "tool_call_id": call["id"],
                "content": result,
            })

            print(
                "TOOL:",
                name,
                "=>",
                result.splitlines()[0],
            )

    else:
        raise RuntimeError(
            "超过最大循环次数"
        )

    # ======================================
    # 10. 实验断言
    # ======================================

    assert "base, remainder" in (
        root / "refund.py"
    ).read_text(
        encoding="utf-8"
    )

    test_calls = [
        m
        for m in messages
        if (
            m["role"] == "tool"
            and m["name"] == "run_tests"
        )
    ]

    assert len(test_calls) == 2
```

---

## 十、实验结果与观察

这段代码已经在 Python 环境中执行验证。

输出为：

```text
TOOL: read_file => def allocate_coupon(total, count):
TOOL: edit_file => UPDATED refund.py
TOOL: run_tests => FAIL (exit=1)
TOOL: edit_file => UPDATED refund.py
TOOL: run_tests => PASS (exit=0)
AGENT: 修复完成，测试通过。
```

这个结果非常关键。

因为我们观察到：

```text
读取文件
    |
    v
第一次修改
    |
    v
测试失败
    |
    v
第二次修改
    |
    v
测试通过
    |
    v
结束
```

现在逐步分析这些行为是谁决定的。

### 10.1 谁决定第一次读取文件？

由：

```python
fake_model(messages)
```

返回：

```python
tool_request(
    "read_file",
    ...
)
```

这是决策部分。

### 10.2 谁真正读取文件？

由 Agent Loop 中：

```python
result = tools[name](**call["args"])
```

执行。

这是行动部分。

### 10.3 为什么第一次测试失败后没有结束？

因为测试结果被记录：

```python
messages.append({
    "role": "tool",
    "name": name,
    "tool_call_id": call["id"],
    "content": result,
})
```

下一轮：

```python
fake_model(messages)
```

能够看到：

```text
FAIL (exit=1)
```

然后选择：

```python
edit_file(...)
```

这就是反馈机制。

### 10.4 为什么第二次测试通过以后结束？

因为模型模拟器返回：

```python
{
    "role": "assistant",
    "content": "修复完成，测试通过。",
    "tool_calls": [],
}
```

而 Loop 中：

```python
if not reply["tool_calls"]:
    break
```

发现已经没有待执行的工具请求。

于是退出。

### 10.5 这个实验真正证明了什么？

它证明：

**一个非常简单的循环，只要能够把行动结果反馈给下一次决策，就可以形成多轮任务执行过程。**

它没有证明：

- Fake Model 具备真实推理能力。
- 第二次修复方案一定适用于生产退款系统。
- 模型一定能准确判断任务是否完成。

这些属于决策质量与任务验证问题。

我们当前验证的只是：

**Loop 的结构和运行机制。**

---

## 十一、为什么这个最小 Loop 还不能直接用于生产？

现在我们已经实现了一个可以运行的 Agent Loop。

但还需要问：

> 如果用真实 LLM 替换 fake_model()，是否就能得到生产级 Coding Agent？

答案是否定的。

因为这个循环仍然存在许多工程问题。

### 11.1 模型返回未知工具怎么办？

例如：

```python
{
    "name": "delete_everything",
    "args": {}
}
```

当前代码：

```python
tools[name]
```

会抛出异常。

我们还没有工具注册验证和错误处理。

### 11.2 模型输出错误参数怎么办？

例如：

```python
{
    "name": "read_file",
    "args": {
        "file_path": 123
    }
}
```

我们还没有可靠的参数 Schema 校验。

### 11.3 测试一直失败怎么办？

当前代码虽然限制最多 12 轮，但并没有完善的：

```text
成本预算
重试策略
失败分类
人工接管
```

### 11.4 模型请求危险命令怎么办？

如果我们增加：

```python
execute(command)
```

模型可能发出危险操作。

当前循环没有权限检查或人工审批。

### 11.5 历史越来越长怎么办？

每一轮都执行：

```python
messages.append(...)
```

当任务持续执行几百轮时，消息列表会不断增长。

我们还没有上下文压缩或历史管理能力。

### 11.6 任务运行中断怎么办？

如果程序执行到：

```text
第一次修改完成
即将运行测试
```

就崩溃了，当前代码无法自动恢复执行。

### 11.7 如何添加这些能力？

当然可以继续往我们的循环里写：

```python
while True:

    check_context()

    response = model(messages)

    validate_tool_calls(response)

    check_permissions(response)

    execute_tools(response)

    save_state()
```

但是这样会使主循环越来越复杂。

新的能力会不断与原有控制逻辑耦合。

这就产生了后续章节要解决的问题：

> **怎样让 Agent Loop 本身保持相对稳定，同时能够不断增加新的行为和基础设施？**

这个问题将引导我们进入 LangChain、LangGraph 和 Middleware。

---

## 十二、回到真实源码：DeepAgents 的 Agent Loop 在哪里？

到目前为止，我们实现的全部是纯 Python。

现在才开始将它与真实源码对应。

固定版本：

```text
DeepAgents 0.7.32
Commit e85c6b9
```

打开：

`libs/deepagents/deepagents/graph.py`

找到：

```python
create_deep_agent(...)
```

在函数结尾可以看到：

```python
return create_agent(
    model,
    ...
    tools=_tools,
    middleware=deepagent_middleware,
    ...
)
```

这段代码说明：

**DeepAgents 并没有在这个入口函数中自行实现一个独立的 Python `while True`。**

它将模型、工具和各种扩展组件交给 LangChain 的：

```python
create_agent(...)
```

我们继续进入：

`langchain/agents/factory.py`

这个文件中可以看到：

```python
graph.add_node("model", ...)
```

以及：

```python
graph.add_node("tools", ...)
```

再通过条件路由将两个节点连接起来。

对于普通 Tool Calling，核心判断包含：

```python
if len(last_ai_message.tool_calls) == 0:
    return end_destination
```

也就是说：

**当最新的模型消息不包含工具调用时，Agent Loop 通常进入结束路径。**

如果有待执行的 Tool Call，就将它们派发到工具节点。

工具执行完成以后，再返回模型节点。

于是我们刚才写出的：

```python
while True:
    ...
```

在真实框架中对应为：

```text
                     START
                       |
                       v
                   Model Node
                       |
                       v
                  Model Output
                       |
                 Has Tool Calls?
                       |
             +---------+---------+
             |                   |
             v                   v
            No                  Yes
             |                   |
             v                   v
            END               ToolNode
                                  |
                                  v
                            Tool Result
                                  |
                                  v
                              Model Node
```

这里要强调：

**Graph 中的 Model Node 和 ToolNode 是执行节点，循环由 LangGraph Runtime 依据路由边调度，而不是 Model Node 递归调用自己。**

这是我们后面要深入研究的重要设计。

---

## 十三、最小实现与真实框架如何对应？

现在我们可以把自己的代码与 LangChain 中的机制建立映射。

| 我们的最小实现 | 真实框架中的对应机制 |
|---|---|
| `fake_model(messages)` | Model Node 中的模型调用 |
| `tools` 字典 | 工具注册与 ToolNode |
| `tool_request(...)` | `AIMessage.tool_calls` |
| `messages.append(reply)` | 模型消息进入 Agent State |
| `tools[name](**args)` | ToolNode 分发并执行工具 |
| `messages.append(tool_result)` | `ToolMessage` 进入 Agent State |
| `if not tool_calls: break` | 模型到结束路径的条件路由 |
| `for` / `while` 循环 | LangGraph 中的循环边与 Runtime 调度 |

这里有一点特别值得注意。

我们的示例中：

```python
messages = []
```

是一个普通列表。

而真实 LangGraph 使用：

```text
State
Channel
Reducer
```

管理消息。

另外，我们的执行器是：

```python
tools[name](**args)
```

真实系统则通过：

```text
ToolNode
ToolRuntime
Tool Schema
Tool Wrapper
```

完成执行。

这些额外机制并没有改变 Agent Loop 最基本的行为。

它们是在解决：

**怎样把这个最小循环可靠地放到真实工程环境中。**

---

## 十四、建立一个更准确的 Agent 架构认识

现在可以回答本章一开始的问题了。

一个最小 Tool Calling Agent 可以理解为：

```text
               Model
                 |
                 v
          Next Action
                 |
                 v
              Tools
                 |
                 v
            Observation
                 |
                 v
               Model
```

但如果我们希望它长期稳定运行，就需要更多机制。

例如：

```text
                      Agent Loop
                          |
          +---------------+---------------+
          |               |               |
          v               v               v
      Model Call       Tool Call       Routing
          |               |               |
          +---------------+---------------+
                          |
                          v
                  Extension Mechanisms
                          |
          +---------------+---------------+
          |               |               |
          v               v               v
       Context          Planning          HITL
       Memory           Subagents         Logging
          |               |               |
          +---------------+---------------+
                          |
                          v
                Runtime Infrastructure
                          |
          +---------------+---------------+
          |               |               |
          v               v               v
        State           Backend       Checkpointer
```

这是一张职责关系图，不表示每个扩展都位于某个固定调用节点。

由此我们得到一个非常重要的认识：

**Agent Loop 是 Tool Calling Agent 的基本执行骨架。**

而：

```text
Middleware
State
Backend
Checkpoint
```

则帮助它具备更丰富的行为、更可靠的执行和更完整的运行时能力。

因此，在这套教程中，我们将以：

```text
Agent Loop
```

为分析中心。

研究其他机制时，首先问：

> **这个机制是在 Loop 的哪个位置接入的？它改变了哪一部分行为？**

如果某个机制不直接属于 Loop 的 Hook，我们再研究它怎样通过 Tool、State 或 Runtime 支撑循环。

这将成为后面所有章节统一的源码阅读方法。

---

## 十五、本章最重要的设计结论

我们从一个退款 Bug 开始，依次遇到了三个问题。

### 第一：为什么一次 LLM 调用不够？

因为任务需要读取文件、修改代码和运行测试。

这些行为无法仅靠生成文本完成。

所以需要：

```text
Tools
```

### 第二：为什么有了 Tools 还不够？

因为第一次执行的结果可能失败。

后续行动必须依赖新观察到的信息。

所以需要：

```text
Feedback
```

### 第三：为什么反馈还不够？

因为需要一个持续运行的控制结构：

```text
模型决定行动
        |
        v
执行行动
        |
        v
记录结果
        |
        v
再次调用模型
```

所以需要：

```text
Agent Loop
```

最终：

```text
                 一次模型调用
                       |
                       v
                无法完成动态任务
                       |
                       v
                    引入工具
                       |
                       v
                工具结果需要反馈
                       |
                       v
                  再次模型决策
                       |
                       v
                    Agent Loop
```

本章最重要的一句话：

> **Agent Loop 的价值，不是重复调用模型，而是让模型能够根据每一次真实行动获得的新信息，持续决定下一步操作。**

---

# 下一章：Agent Loop 的通信协议

现在，我们已经写出一个最小 Agent Loop。

但示例中的工具调用只是普通字典：

```python
{
    "name": "read_file",
    "args": {
        "file_path": "refund.py"
    }
}
```

在真实 LangChain 中，这些数据有自己的结构化协议。

例如：

```python
AIMessage
ToolMessage
tool_calls
tool_call_id
```

那么接下来要研究的问题就是：

**模型的行动请求与工具执行结果，为什么必须采用结构化的消息协议，而不能只是互相传递字符串？**

这将是第二章的起点。

下一章重点阅读：

```text
langchain_core.messages.AIMessage
langchain_core.messages.ToolMessage
langchain_core.tools.BaseTool
```

进一步理解：

```text
Model
  |
  v
AIMessage(tool_calls)
  |
  v
Tool Execution
  |
  v
ToolMessage
  |
  v
Model
```

为第三章研究 LangGraph 如何把这个循环构建为 StateGraph 做准备。

---

## 本章源码与参考资料

### DeepAgents

固定版本：

`0.7.32 / e85c6b9`

入口：

`libs/deepagents/deepagents/graph.py`

重点阅读：

```python
create_deep_agent(...)
```

以及最终：

```python
create_agent(...)
```

源码：

https://github.com/langchain-ai/deepagents/blob/e85c6b9/libs/deepagents/deepagents/graph.py

### LangChain

入口：

`libs/langchain_v1/langchain/agents/factory.py`

本章只需要定位：

```text
create_agent()
model_node()
_make_model_to_tools_edge()
_make_tools_to_model_edge()
```

暂时不需要逐行研究 Middleware 编译逻辑。

源码：

https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/factory.py

注意：LangChain 链接指向主分支。深入逐行分析时需要固定与 DeepAgents 版本兼容的实际依赖版本。

### 理论参考

ReAct: Synergizing Reasoning and Acting in Language Models

https://arxiv.org/abs/2210.03629

本章借鉴其推理—行动—观察交替的思想，但不要求所有 Agent 都显式输出推理文本。

---

## 本章自检

读完本章，应能够不看源码回答：

1. 为什么一次 LLM 调用不等于完成一个任务？
2. Workflow 也可以有循环，为什么还需要 Agent？
3. Tool Call 与工具真实执行有什么区别？
4. 为什么工具结果必须反馈给下一轮模型？
5. 一个最小 Agent Loop 需要哪些组成部分？
6. Agent Loop 的结束条件与业务任务成功是否等价？
7. 为什么真正的 Agent Harness 不能只有一个 `while True`？
8. DeepAgents 为什么会委托 LangChain 构建 Agent Loop？

如果能够回答这些问题，再进入 LangChain 的消息协议和 Graph 构建源码，就会更加自然。
