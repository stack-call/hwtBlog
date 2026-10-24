---
title: 第十二章：Execution Environment——工具在哪儿执行？
sidebar_label: 第十二章：Execution Environment
description: 通过意外修改宿主文件、残留进程和测试隔离推导工作区与 Sandbox
sidebar_position: 13
tags: [Agent, Harness, Sandbox, Codex, OpenHands]
---

# 第十二章：Execution Environment——为什么不能直接调用宿主 Shell？

## 事故：修复测试时清理了错误目录

Agent 要运行 `pytest`。小李最初的工具实现只是 `subprocess.run(command, shell=True)`，工作目录跟随服务进程的当前目录。

当模型生成清理命令，目标路径解释错了，测试以外的文件也被删除。此时 Tool Schema 完全合法；Agent Loop 也没有任何 bug。出问题的是：**执行环境默认拥有过多能力**。

小李认识到，**工具可以被调用**和**工具能够接触哪些资源**是两道独立边界。

## 第一层补救：限制工作目录

他让每个任务拥有自己的 `workspace_root`，读取、写入和 Patch 工具只能操作工作目录中的文件。

但出现了新问题：一个命令可以通过 `../`、符号链接或绝对路径越界；Shell 也可能访问环境变量中的凭据，或者访问互联网。单靠 `cwd` 不是安全隔离。

下一步需要 OS 级隔离：容器、进程沙箱、受限文件系统视图、网络策略以及资源配额。

```text
Agent Tool Call
  → Policy Check
  → Execution Request (cwd/env/timeout/budget)
  → Sandbox Boundary
  → Process + File + Network Isolation
  → stdout/stderr/exit_code/artifacts
```

## 第二层故障：测试通过，但污染了下一次任务

优惠券项目的测试生成了临时数据库与日志。下一个用户复用同一容器时，读到了上一轮的测试残留。

因此 **Sandbox** 和 **Workspace Lifecycle** 也不是一个概念：

- Sandbox：限定进程可以做什么，保护宿主与其他任务；
- Workspace：确定本次任务操作哪些文件，如何初始化、复用或清理；
- Session：追踪用户任务逻辑上的连续性；它不应自动成为全局共享磁盘目录。

什么时候应该复用工作区？同一个 Coding Agent 在修 Bug 时需要保留刚修改的代码；什么时候应该隔离？不同用户、不同仓库和测试分支之间的内容不应混用。

## 第三个故障：执行超时，子进程没有退出

Harness 取消 `pytest` 等待任务后，Python 协程被取消了，但底层测试进程仍在运行、继续写文件。**取消等待，不等于终止进程树。**

一个生产执行器至少要考虑：明确 Process Group、超时、终止信号、优雅退出/强制退出、输出截断和资源上限。执行结果要说明命令是自然退出还是被中断，避免模型误将“无错误输出”理解为“成功”。

```python
# 教学片段：强调分层，不是完整安全沙箱
request = ExecRequest(
    command=["pytest", "tests/test_refund.py"],
    cwd=workspace.root,
    timeout_seconds=60,
)
result = await sandbox.execute(request)
```

## 真实代码去哪里看？

1. [Codex `tools/orchestrator.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/orchestrator.rs)：重点看 `run` 如何协调 Tool Runtime 与批准/隔离执行。这是工具调用与策略的交汇点，而不是某种 OS 沙箱全部逻辑所在的文件。
2. [Codex `codex-rs` 源码树](https://github.com/openai/codex/tree/main/codex-rs)：继续检索 `sandbox`、`exec` 等模块，区分策略选择与平台级强制执行。
3. [OpenHands 源码仓库](https://github.com/All-Hands-AI/OpenHands)：从 Runtime 与执行工具入口追踪命令如何进入独立执行环境。具体子目录随版本重构，建议通过仓库搜索定位而不是记死旧路径。

**关键取舍**：Sandbox 的性能、可复用性、文件同步成本、调试便利与安全隔离强度之间存在冲突。对不可信代码，不能用“Prompt 中写禁止删除文件”替代操作系统级控制。

| 五个问题 | 回答 |
|---|---|
| 是什么 | 工具执行环境承载 Shell、文件、测试和浏览器等操作。 |
| 为什么需要 | 限定副作用范围、隔离用户任务、管理进程资源。 |
| 没有它 | 合法 Tool Call 仍可损坏宿主、泄露密钥或跨会话污染。 |
| 实际怎么做 | Workspace + Sandbox + 超时/资源控制 + 日志和产物管理。 |
| 处于哪里 | Tool Runtime 之下，与 Policy Engine 共同约束可执行能力。 |

### 实践验证

创建两个任务工作区，分别运行会产生 `output.json` 的测试；验证互不可见。再测试符号链接越界、超时子进程清理、网络断开、无写权限情况下的错误回传。实验应在安全的临时目录或容器里进行。

**下一章的问题**：即使沙箱本身安全，某些真正影响用户或生产环境的操作是否应该先问人？
