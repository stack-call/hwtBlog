---
title: 第十二章：Execution Environment——为什么不能直接调用宿主 Shell？
sidebar_label: 第十二章：执行环境
description: 通过意外修改宿主文件、残留进程和测试隔离推导工作区与 Sandbox
sidebar_position: 13
tags: [Agent, Harness, Sandbox, Codex, OpenHands]
---



# 第十二章：Execution Environment——Agent 的代码究竟在哪里执行？

上一章，我们设计了 Tool System。

现在 Coding Agent 已经能够：

1. 接收模型产生的 Tool Call。
2. 找到对应的 Tool。
3. 验证参数与执行权限。
4. 执行工具。
5. 将结果交回 Agent Loop。

于是小李写出了这样一段代码：

```python
def execute(command):
    return subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
    )
```

模型请求：

```text
execute("pytest tests/test_refund.py")
```

Harness 调用 `subprocess.run()`，测试成功执行。

到这里，一切正常。

但真正的问题是：

**这条命令究竟运行在哪里？它能够访问什么？如果它破坏了环境，谁来承担后果？**

我们继续从这个简单实现开始，让问题一步步出现。

---

## 一、第一次事故：一个正常的测试命令，为什么会修改不该修改的文件？

Coding Agent 正在修复优惠券退款 Bug。

它已经修改了 `refund.py`，准备运行测试：

```text
pytest tests/test_refund.py
```

这是一条完全合理的命令。

Tool Registry 能找到 `execute`，参数合法，权限检查也通过了。

但测试执行时，项目里的某个初始化脚本意外连接到了共享测试数据库，并清空了其中一张表。

问题出现了。

小李最初的反应是：

“是不是 Permission System 没设计好？”

但仔细分析后发现，Permission System 只判断了：

> 当前 Agent 是否可以调用 Shell 工具？

它并没有保证：

> Shell 进程启动后，它以及它执行的程序，只能访问指定的文件、网络和数据库。

这是两个不同的问题。

### Tool Permission 不等于执行隔离

假设我们已经允许执行：

```text
pytest tests/test_refund.py
```

实际执行链可能是：

```text
Harness
   |
   v
Shell
   |
   v
pytest
   |
   +--> import conftest.py
   |
   +--> 执行测试初始化脚本
   |
   +--> 启动其他进程
   |
   +--> 连接数据库
```

即使最初的命令没有明显危险，其内部执行的代码也可能产生副作用。

因此：

**控制 Agent 能不能调用某个工具，不等于控制这个工具运行后的实际影响范围。**

我们需要再向下增加一道边界。

但先不着急引入 Sandbox，看看能否用更简单的办法解决。

---

## 二、第一次改进：限制 Working Directory

小李发现，原来的 `subprocess.run()` 没有指定工作目录。

于是命令可能在 Harness 的启动目录下执行。

如果服务运行在：

```text
/home/server/
```

而项目实际位于：

```text
/workspaces/coupon-project/
```

那么相同的相对路径会对应不同的位置。

小李修改了代码：

```python
def execute(command, workspace):
    return subprocess.run(
        command,
        shell=True,
        cwd=workspace,
        capture_output=True,
        text=True,
    )
```

现在工具默认在项目目录执行。

例如：

```text
cwd = /workspaces/coupon-project

pytest tests/test_refund.py
```

这样至少保证项目相对路径具有明确含义。

我们直到这里才给这个概念命名：

**Working Directory（当前工作目录）**。

它解决的是命令执行时的路径定位问题。

### 但 cwd 能限制文件访问吗？

小李做了一个实验。

虽然命令在项目目录运行，但程序仍可以读取其操作系统账户有权访问的其他绝对路径。

例如，一个进程完全可以使用：

```python
open("/home/server/private-config.txt")
```

只要操作系统允许访问，该文件就仍然可以被读取。

所以：

**`cwd` 不是安全边界。**

它只是文件路径解析的默认位置。

### 对照 DeepAgents 的真实实现

打开：

[DeepAgents local_shell.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/backends/local_shell.py)

`LocalShellBackend` 的 Shell 执行使用 Python `subprocess.run()`，其中包含：

```python
shell=True
cwd=str(self.cwd)
```

这是其执行参数中的关键部分。

但注意，源码非常明确地说明：

`LocalShellBackend` 在宿主机直接执行命令，**不提供 Sandbox 隔离**。

它可以设置 `root_dir` 和 `virtual_mode`，但针对文件工具的虚拟路径限制，并不能约束 Shell 命令访问宿主机其他文件。

为什么要专门提醒这一点？

因为我们自己刚才就推导出了这个陷阱。

如果把“设置根目录”和“限制进程权限”理解成一回事，就会产生错误的安全假设。

---

## 三、第二次事故：两个 Agent 为什么会互相覆盖代码？

现在小李的 Coding Agent 已经投入团队使用。

两个用户同时提交任务。

用户 A：

> 修复优惠券退款 Bug。

用户 B：

> 重构订单金额计算模块。

两个 Agent 都在同一个项目目录下执行：

```text
/workspaces/order-system/
```

Agent A 正在修改：

```text
src/refund.py
```

Agent B 也需要修改：

```text
src/refund.py
```

结果 Agent A 刚修改完成，Agent B 就覆盖了它的部分代码。

更糟糕的是，Agent A 运行测试时，实际测试的是一个混合了两个人修改的工作目录。

于是出现了一个新问题：

**即使工具都在正确的项目目录执行，也不意味着不同 Agent 的工作互不干扰。**

### 第二次改进：为任务分配独立 Workspace

小李决定：

每个独立任务使用自己的工作区。

```text
Repository
    |
    +-- Task A Workspace
    |      |
    |      +-- refund.py
    |      +-- coupon.py
    |
    +-- Task B Workspace
           |
           +-- refund.py
           +-- coupon.py
```

一种具体实现方式是 Git Worktree：

```bash
git worktree add ../task-a -b task-a
git worktree add ../task-b -b task-b
```

然后：

```text
Agent A
  cwd = ../task-a

Agent B
  cwd = ../task-b
```

现在两个 Agent 修改的文件路径属于不同工作区。

它们不再直接覆盖对方的工作树。

这里我们引入：

**Workspace Isolation（工作空间隔离）**。

它解决的是不同任务之间的文件修改冲突。

但我们要继续追问一层。

### Workspace Isolation 等于 Sandbox 吗？

仍然不是。

Git Worktree 隔离了工作树，但两个进程可能仍然：

- 使用同一个操作系统用户。
- 访问同一个 Home 目录。
- 读取同一组环境变量。
- 使用同一个网络。
- 连接同一个数据库。
- 修改同一份全局缓存。

因此：

> Workspace 隔离解决文件工作区冲突，但不自动提供进程、网络或系统权限隔离。

到这里，我们已经发现 `cwd` 和 Workspace 都无法完整解决最初的执行风险。

我们终于需要引入更强的机制。

---

## 四、第三次改进：让代码运行在受限制的环境中

小李重新思考问题。

我们真正希望控制的，并不是命令字符串本身，而是命令执行后的能力范围。

例如：

```text
允许：
- 读取项目代码
- 修改工作区文件
- 在工作区运行测试
- 访问必要的依赖

限制：
- 读取宿主机敏感文件
- 修改工作区之外的路径
- 任意访问内部网络
- 消耗无限 CPU、内存和磁盘
```

这时，他意识到：

**与其要求模型永远不产生危险命令，不如让执行环境本身限制命令能够产生的影响。**

这就是 Sandbox（沙箱）的核心思路。

### Sandbox 不只是一个临时目录

一个 Sandbox 可能通过多种机制实现隔离：

| 隔离维度 | 解决什么问题 |
|---|---|
| Filesystem Isolation | 控制可读写路径 |
| Process Isolation | 控制进程可见性与影响范围 |
| Network Isolation | 限制网络访问 |
| Identity Isolation | 使用受限制的操作系统身份 |
| Resource Limits | 限制 CPU、内存、进程数等 |
| Environment Isolation | 控制环境变量、依赖和凭据 |

但需要注意：

**Sandbox 并不是一个单一技术，也不是开启某个参数就绝对安全。**

不同实现提供的隔离强度不同。

例如容器、操作系统 Sandbox、虚拟机、远程执行服务，都可能承担这个角色。

### 对照 Codex：操作系统级 Sandbox

打开：

[Codex Linux Sandbox](https://github.com/openai/codex/tree/main/codex-rs/linux-sandbox)

以及：

[Codex linux-sandbox/README.md](https://github.com/openai/codex/blob/main/codex-rs/linux-sandbox/README.md)

Codex 在不同平台采用不同的隔离机制。

在 Linux 上，当前实现使用 Bubblewrap，并配合 Seccomp 等机制控制进程和系统调用。

例如，Bubblewrap 可以先创建只读的文件系统视图，再针对允许的工作目录开放写权限。

其核心思想可以简化成：

```text
默认文件系统：只读
        |
        +-- Workspace：允许写入
        |
        +-- 指定敏感目录：继续禁止访问或修改
```

在 macOS 上，Codex 使用 Seatbelt 对进程执行进行约束。

为什么要在操作系统层面做这些工作？

因为 Shell 命令可能通过 Python、Node.js、编译器或其他子进程执行任意逻辑。

只检查最初的工具参数，无法可靠控制后续所有系统调用。

所以：

**Sandbox 的重要价值，是把部分安全约束下沉到进程实际运行的环境中。**

这也解释了为什么 Sandbox 和 Permission System 应该相互配合，而不是互相替代。

---

## 五、第四次改进：为什么 DeepAgents 不直接实现一个固定的 Sandbox？

小李准备把自己的 Coding Agent 改成支持隔离执行。

但他遇到了一个新的产品要求：

有些用户希望本地执行。

有些用户希望用 Docker。

有些企业希望把 Agent 放进自己的远程虚拟机。

如果在 Tool System 里写死一种 Sandbox 实现，那么每增加一种部署环境，都需要修改执行逻辑。

小李想到：

“能不能让 Agent 只依赖一个统一的执行接口，而由不同 Backend 决定命令在哪里运行？”

于是他设计：

```python
class ExecutionBackend:
    async def execute(self, command, timeout):
        ...

    async def read_file(self, path):
        ...

    async def write_file(self, path, content):
        ...
```

这是教学上的简化接口。

真正实现时还需要文件操作结果、错误类型、取消能力、资源限制等约定。

现在架构变成：

```text
                 Agent Loop
                     |
                     v
                 Tool System
                     |
                     v
              Execution Backend
                     |
         +-----------+-----------+
         |           |           |
         v           v           v
      Local       Container    Remote VM
```

Agent Loop 不需要知道具体的执行位置。

### 对照 DeepAgents 的 Backend 抽象

打开：

[DeepAgents backends/protocol.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/backends/protocol.py)

其中有一个关键抽象：

```python
class SandboxBackendProtocol(BackendProtocol):
    ...
```

它在基础文件操作能力之上，增加了：

```python
execute(command, *, timeout=None)
aexecute(command, *, timeout=None)
```

这是源码中的真实接口名称。

我们需要特别理解：**`SandboxBackendProtocol` 描述的是执行能力契约，并不能单凭类名证明具体实现一定具有安全隔离。**

例如前面提到的 `LocalShellBackend` 也实现了相应执行接口，但实际命令运行在本地宿主机上。

因此，选择 Backend 时必须检查具体实现的安全属性。

在 DeepAgents 的 `FilesystemMiddleware` 中，也可以看到这样的能力区分：

- 普通 Backend 可以提供文件操作。
- 支持执行接口的 Backend 才提供相应 Shell 执行能力。

于是我们获得了两个重要的设计原则：

**Tool System 不应该关心命令具体运行在哪台机器。**

**Execution Backend 必须明确自己支持哪些能力，以及具有怎样的隔离保证。**

---

## 六、第五次事故：命令卡住了，用户点击停止为什么没反应？

现在 Sandbox 已经解决了部分隔离问题。

Agent 再次运行测试：

```text
execute("pytest tests/test_refund.py")
```

但这次某个测试进入了死循环。

进程一直不退出。

Agent Loop 正在等待：

```python
result = await execute_tool(call)
```

用户点击停止。

可是执行环境中的测试进程仍然运行。

为什么？

因为**取消 Agent 的逻辑任务，不等于自动结束已经启动的操作系统进程**。

### 第五次改进：Process Lifecycle

这时，小李意识到，Execution Environment 不仅要能够启动进程，还必须管理进程生命周期。

至少需要处理：

```text
CREATED
   |
   v
RUNNING
   |
   +--> COMPLETED
   |
   +--> FAILED
   |
   +--> TIMED_OUT
   |
   +--> CANCELLED
```

例如：

```text
Agent Loop
   |
   v
Tool Runtime
   |
   v
Process Manager
   |
   +-- 启动进程
   +-- 收集 stdout / stderr
   +-- 等待退出
   +-- 处理超时
   +-- 处理取消
   +-- 清理子进程
```

为什么不能只调用一次 `process.kill()`？

因为 Shell 可能启动其他子进程。

```text
Shell Process
     |
     +-- pytest
            |
            +-- worker 1
            |
            +-- worker 2
```

只终止父进程，不一定能够终止所有子进程。

所以执行环境可能需要管理进程组、进程树或容器级生命周期。

### 对照 OpenCode 的真实源码

OpenCode 的进程管理很值得研究。

阅读：

[OpenCode core/shell.ts](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/shell.ts)

其中的 `killTree()` 提供了跨平台的进程树终止处理。

在类 Unix 系统的相关分支中，可以看到类似：

```typescript
process.kill(-pid, "SIGTERM")
```

之后，在需要时再使用更强制的终止信号。

为什么要使用负的 PID？

在适当的进程组设置下，这可以向整个进程组发送信号，而不只是向某一个进程发送信号。

但这里有一个前提：

进程必须以符合该逻辑的进程组方式启动。并且，即使如此，也不意味着所有已经脱离进程组的后代都一定会被清理。

OpenCode 的这类实现说明：

**Tool System 中的取消信号，最终必须被 Execution Environment 转换成真正的进程控制操作。**

### Timeout 与 Cancellation 不是同一回事

Timeout 表示：

> 命令超过了规定的执行时间。

Cancellation 表示：

> 外部主动要求终止当前执行。

两者最终都可能需要终止进程，但来源不同。

另外还要注意，进程结束不一定意味着输出流已经关闭。子进程可能继承 stdout/stderr 的文件描述符，导致读取过程继续等待。

因此成熟执行器还需要考虑输出流关闭、终止超时和资源回收，而不能只关注主进程退出码。

---

## 七、第六次改进：为什么执行环境必须有自己的生命周期？

小李现在已经支持：

- 创建 Workspace。
- 启动 Sandbox。
- 执行命令。
- 超时和取消。

但用户要求 Coding Agent 连续执行几个小时。

Agent 需要多次运行：

```text
pip install -r requirements.txt
pytest
python scripts/check_refund.py
```

如果每次命令都创建一个全新的环境，那么每轮都可能需要重新安装依赖。

这会浪费大量时间。

反过来，如果所有任务永远共享同一个环境，又可能出现依赖污染和任务间冲突。

于是问题变成：

**执行环境到底应该存在多久？**

小李开始设计 Sandbox Lifecycle。

```text
Task Created
     |
     v
Provision Environment
     |
     v
Initialize Workspace
     |
     v
Execute Tool Calls
     |
     +--> Execute
     +--> Execute
     +--> Execute
     |
     v
Collect Artifacts
     |
     v
Destroy / Recycle Environment
```

这就引出了 **Execution Environment Lifecycle**。

一种常见策略是：

一个任务对应一个独立的执行环境，在任务期间复用，在任务完成后销毁或回收。

但不是所有 Harness 都必须如此。

例如：

- 本地 Coding Agent 可能复用用户已有的工作区。
- 云端 Agent 可能为每个任务创建短期容器。
- 长时间运行的 Agent 可能使用可暂停的远程环境。
- 并行子任务可能需要分别创建工作区或 Sandbox。

真正重要的是，明确环境与 Session、Turn、Task 的生命周期关系。

### 对照 OpenHands：远程 Runtime

打开：

[OpenHands Runtime Architecture](https://github.com/OpenHands/docs/blob/main/openhands/usage/architecture/runtime.mdx)

在 OpenHands 经典 Docker Runtime 架构中：

```text
OpenHands Backend
       |
       v
  Event Stream
       |
       v
 Runtime Client
       |
       v
Docker Container
       |
       v
 Action Executor
       |
       +-- Bash
       +-- Browser
       +-- Python / Plugins
```

Agent 的动作被送到容器内执行。

容器返回 Observation，再交给 Agent。

为什么这种架构很重要？

因为它把 Agent 的推理逻辑与具体的代码运行环境隔离开来。

它也使得不同任务可以拥有不同的运行时镜像、依赖与工作区。

这里的设计重点不是 Docker 本身，而是：

**Agent Runtime 负责产生行动，Execution Runtime 负责在指定环境里真正执行行动。**

但 Docker 是否足够安全仍然取决于实际配置，例如特权模式、宿主机目录挂载、Docker Socket、网络和资源限制。容器本身不能替代完整的威胁模型。

---

## 八、第七次事故：换了 Sandbox，为什么测试结果不一样？

现在小李将测试放进独立环境执行。

但是同一份代码在两个 Sandbox 中出现了不同结果：

```text
Sandbox A:
Python 3.11
pytest 8.x
All tests passed

Sandbox B:
Python 3.12
pytest 9.x
1 test failed
```

这次没有发生越权访问，也没有进程冲突。

问题出在环境差异。

小李发现：

**执行环境除了提供隔离，还必须保证结果具有足够的可复现性。**

于是他开始记录：

```text
Runtime Image
Python Version
Dependency Lockfile
Environment Variables
Working Directory
Test Database / Fixtures
Network Dependencies
```

例如：

```text
runtime_image = python:3.12
dependencies = requirements.lock
workspace_revision = abc123
test_database = isolated-test-db
```

这里要注意，镜像标签可能改变其指向的具体内容。需要严格复现时，应进一步固定镜像 Digest、依赖版本及外部服务状态。

这就是 **Environment Reproducibility（环境可复现性）**。

它与 Sandbox 的关系是：

- Sandbox 主要限制执行的影响范围。
- Reproducibility 主要减少执行环境的非预期差异。

两者常常一起设计，但不是同一个概念。

---

## 九、还有一个容易忽略的问题：Sandbox 能回滚一切吗？

我们的 Coding Agent 在 Sandbox 中运行：

```text
pytest tests/test_refund.py
```

测试脚本访问了外部数据库。

Sandbox 随后被销毁。

那么外部数据库中的变更会自动回滚吗？

**不会。**

因为 Sandbox 控制的是自身执行环境，不一定控制外部服务。

如果 Agent 能够访问：

```text
External Database
External API
Shared Storage
```

那么这些系统中的副作用可能在 Sandbox 销毁后继续存在。

这意味着：

**Sandbox Isolation 不等于事务隔离，也不等于业务操作可回滚。**

如果需要安全执行测试，我们还需要：

- 测试专用数据库和凭据。
- 网络访问控制。
- 对敏感外部操作进行权限审批。
- 对不可重复操作使用幂等机制。
- 对可回滚的修改提供快照或事务机制。

这里就与之前学过的 Checkpoint、Idempotency 和 Permission System 联系起来了。

Checkpoint 可以恢复 Agent 的逻辑状态，但不保证外部世界自动恢复。

Sandbox 可以限制进程能力，但不保证外部系统没有被修改。

权限审批可以决定是否允许执行，但不意味着被批准的操作不会出现意外后果。

三个机制各自解决不同问题。

---

## 十、现在我们可以设计完整的 Execution Environment

经过这些工程问题，小李最终得到下面的架构：

```text
                  Agent Loop
                      |
                      v
                  Tool System
                      |
                      v
                Permission Check
                      |
                      v
               Execution Manager
                      |
          +-----------+-----------+
          |           |           |
          v           v           v
     Workspace     Process     Sandbox
      Manager      Manager     Manager
          |           |           |
          +-----------+-----------+
                      |
                      v
                Execution Backend
                      |
          +-----------+-----------+
          |           |           |
          v           v           v
        Local      Container    Remote VM
                      |
                      v
                 Tool Result
                      |
                      v
                  Agent Loop
```

注意：这是一张逻辑职责图，不代表所有项目都有这些同名类。

我们真正关心的是以下几类责任：

| 组件 | 责任 |
|---|---|
| Workspace Manager | 管理任务代码与文件修改范围 |
| Process Manager | 管理命令启动、等待、取消和清理 |
| Sandbox Manager | 管理隔离策略和环境生命周期 |
| Execution Backend | 屏蔽本地、容器与远程执行差异 |
| Execution Result | 统一返回输出、退出码和错误状态 |

一个合理的执行请求可以包含：

```python
@dataclass
class ExecutionRequest:
    command: str
    workspace_id: str
    sandbox_id: str
    timeout_seconds: int
    max_output_bytes: int
```

这是教学级数据结构，真实系统还需要明确授权主体、工作目录、环境变量、取消上下文和可执行策略。

特别注意：

`workspace_id` 和 `sandbox_id` 应由 Harness 根据可信的任务状态确定，而不是让模型任意指定其他用户的执行环境。

### Execution Result 应该返回什么？

假设我们只返回：

```text
FAIL test_refund
```

那么 Agent Loop 无法判断：

- 命令是否真正启动。
- 进程是否超时。
- 输出是否被截断。
- Sandbox 是否失效。
- 是否因为用户取消而退出。

因此，我们需要统一的执行结果协议。

例如：

```python
@dataclass
class ExecutionResult:
    exit_code: int | None
    output: str
    timed_out: bool
    cancelled: bool
    truncated: bool
```

同样，这只是简化设计。

当结果太大时，完整日志还可以交给之前的 Artifact Store，模型只接收摘要或文件引用。

因此，Execution Environment 与 Context Management 也不是互相独立的。

Execution Environment 产生大量工具输出，Context Management 决定哪些内容应该进入模型上下文。

---

## 十一、四个开源项目的架构对比

现在回头看 Codex、DeepAgents、OpenCode、OpenHands。

它们在 Execution Environment 上的取舍非常不同。

| 项目 | 主要设计思路 | 最值得研究的部分 |
|---|---|---|
| Codex | 本地执行与操作系统级 Sandbox 结合 | 文件、进程、网络权限的实际约束 |
| DeepAgents | 使用 Backend 抽象执行环境 | 文件与 Shell 能力解耦、执行 Backend 可替换 |
| OpenCode | 围绕本地 Shell、会话和进程管理构建执行能力 | 子进程启动、超时、终止、输出处理 |
| OpenHands | 将执行动作发送到独立 Runtime | Runtime 生命周期、容器执行、Action/Observation 边界 |

### Codex：关注安全约束在哪里生效

从 Codex 中，我们学到：

一个命令即使获得执行许可，仍然应该受到操作系统实际执行边界的约束。

重点不在于 Registry 中如何描述命令，而在于命令启动后能够访问哪些资源。

### DeepAgents：关注如何替换执行环境

从 DeepAgents 中，我们学到：

Agent 的 Tool System 可以依赖统一 Backend 接口，而不是写死本地文件系统或 Shell。

但接口是否支持执行，与执行是否受到安全隔离，是两个不同的判断。

### OpenCode：关注进程执行的工程细节

从 OpenCode 中，我们学到：

即使不考虑复杂的容器编排，一个可靠的本地执行器仍然需要处理进程树、超时、取消、输出和会话关联。

这些不是外围细节，而是 Tool Runtime 能否稳定运行的必要条件。

### OpenHands：关注执行环境的独立生命周期

从 OpenHands 中，我们学到：

当 Agent 需要执行任意项目代码时，把执行动作发送给独立 Runtime，有利于统一隔离、依赖管理和执行状态管理。

这种设计也更适合把远程环境创建、复用、销毁与 Agent Loop 解耦。

---

## 十二、什么时候应该选择哪种执行方式？

不要一开始就假设所有 Agent 都必须使用 Docker 或虚拟机。

应该先从威胁模型和任务需求出发。

| 场景 | 可以考虑的执行方式 |
|---|---|
| 个人本地、可信项目开发 | 本地执行 + 明确审批与备份 |
| 多任务并行编码 | 独立 Worktree / Workspace |
| 需要运行不可信项目代码 | 经过加固的 Sandbox |
| 多租户云端 Coding Agent | 每任务隔离的容器或虚拟机 + 资源和权限限制 |
| 长时间运行的开发任务 | 支持复用、暂停与回收的持久执行环境 |

以上是架构选择的起点，不是无条件的安全保证。

例如，即使每个任务都有独立 Docker 容器，也仍然需要检查挂载、网络、用户权限和资源配置。

真正要避免的误区是：

> 创建了一个容器，就认为所有执行问题都解决了。

Sandbox 负责的是有限范围内的隔离保证，而不是业务正确性、资源管理、恢复一致性和所有外部副作用。

---

## 十三、本章的五个核心问题

### 1. Execution Environment 是什么？

它是 Agent Harness 中负责将工具请求转换成实际操作，并管理工作目录、进程、资源和运行环境的系统。

### 2. 为什么需要？

因为模型产生的动作最终要在真实操作系统或远程环境中执行，而这些动作可能影响文件、进程、网络和外部服务。

### 3. 没有它会怎样？

简单情况下，可以直接使用 `subprocess.run()`。

但当任务复杂化、用户增加、执行时间变长时，会出现工作区冲突、进程泄漏、环境污染、越权访问和结果不可复现等问题。

### 4. 实际怎么做？

从最小的 `cwd + subprocess` 开始，逐步增加：

```text
Workspace
    |
Process Management
    |
Sandbox Isolation
    |
Execution Backend
    |
Lifecycle Management
```

每个机制都应该由真实问题驱动，而不是一开始全部引入。

### 5. 在整个 Harness 中处于什么位置？

Execution Environment 位于 Tool System 之下，是实际产生外部副作用的执行层。

```text
Agent Loop
    |
    v
Tool System
    |
    v
Execution Environment
    |
    v
Operating System / External Services
```

Tool System 管理“调用什么能力”。

Execution Environment 管理“能力在哪里、以什么权限、通过什么进程执行”。

Permission System 管理“是否允许该操作”。

这些是互相配合的职责，而不是能够互相替代的机制。

---

## 十四、独立验证：迁移到一个新的 Agent 场景

假设现在不再做 Coding Agent，而是开发一个 Data Analysis Agent。

用户上传一个 Python 分析脚本：

> 运行这个脚本，生成销售预测图表。

这个脚本可能：

- 安装额外依赖。
- 读取输入数据。
- 生成图像。
- 运行很久。
- 意外读取外部路径。
- 占用过多内存。

现在我们已经不需要重新推导全部概念。

可以直接从问题出发，逐步设计：

**第一步：Workspace**

为每个用户任务分配独立工作目录。

**第二步：Execution Backend**

选择能够执行 Python 脚本的环境。

**第三步：Sandbox**

控制脚本的文件、网络和资源访问范围。

**第四步：Process Management**

处理超时、取消、输出和异常退出。

**第五步：Artifact Management**

将生成的图表作为文件保存并返回引用，而不是把整个二进制数据放进聊天历史。

**第六步：Lifecycle**

任务完成后，清理进程、销毁或回收运行环境，按策略保存所需结果。

能够把这些机制迁移到不同项目，才说明真正理解了 Execution Environment 的设计原因。

---

## 本章总结

我们最初只有：

```python
subprocess.run(command, shell=True)
```

它能执行命令，却无法回答一系列重要问题：

- 命令属于哪个任务？
- 当前目录是什么？
- 是否会影响其他任务？
- 进程能访问什么？
- 出现死循环怎么办？
- 用户取消后如何清理？
- 执行环境能否复用？
- 结果能否复现？

随着这些问题出现，我们逐步引入了 Workspace、Process Manager、Sandbox、Backend 和 Lifecycle。

最终得到一个核心认识：

**Tool System 解决 Agent 如何调用能力；Execution Environment 解决这些能力如何在受控环境中真正执行。**

因此，Execution Environment 并不只是“运行 Shell 的一个函数”，而是 Agent Harness 与真实计算资源之间的运行边界。

## 真实源码阅读路线

建议先围绕具体问题阅读，不必逐行通读。

### DeepAgents：从本地执行到 Backend 抽象

- [local_shell.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/backends/local_shell.py)：理解 `cwd`、Shell 执行与没有 Sandbox 的风险。
- [protocol.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/backends/protocol.py)：理解 `SandboxBackendProtocol` 的能力边界。
- [sandbox.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/backends/sandbox.py)：理解 Sandbox Backend 如何封装执行与文件操作。

### Codex：理解操作系统级隔离

- [linux-sandbox/README.md](https://github.com/openai/codex/blob/main/codex-rs/linux-sandbox/README.md)：理解 Bubblewrap、Seccomp、文件系统限制。
- [linux_run_main.rs](https://github.com/openai/codex/blob/main/codex-rs/linux-sandbox/src/linux_run_main.rs)：理解 Sandbox Helper 如何组织启动过程。
- [core/README.md](https://github.com/openai/codex/blob/main/codex-rs/core/README.md)：理解不同操作系统的执行前提。

### OpenCode：理解 Shell 与进程生命周期

- [core/shell.ts](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/shell.ts)：重点读 `killTree()`、Shell 选择和进程控制。
- [core/tool/AGENTS.md](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/tool/AGENTS.md)：理解 Core Tool 的执行与注册职责。
- [core/tool/tool.ts](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/tool/tool.ts)：理解工具执行上下文与结果契约。

### OpenHands：理解独立 Runtime

- [Runtime Architecture](https://github.com/OpenHands/docs/blob/main/openhands/usage/architecture/runtime.mdx)：理解 Action Executor、Docker Runtime 与 Observation 的交互。
- [Docker Runtime 文档](https://github.com/OpenHands/docs/blob/main/openhands/usage/v0/runtimes/V0_docker.mdx)：理解工作区挂载和容器执行配置。

以上链接对应持续演进的仓库。正式进行源码研究时，建议固定一个 Commit，避免版本变化导致接口或文件路径不一致。

---

**下一章：Permission & Approval——既然已经有了 Sandbox，为什么 Agent 执行危险操作前还需要用户审批？**

我们会继续从优惠券 Coding Agent 出发：某次模型请求执行一个可能丢失用户修改的 Git 命令。Sandbox 允许它修改工作区，但用户并没有同意丢失这些修改。

这个问题将迫使我们区分三个层次：

**技术上能做什么、系统策略允许做什么，以及用户是否同意做这件事。**
初的工具实现只是 `subprocess.run(command, shell=True)`，工作目录跟随服务进程的当前目录。

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

## 本章归位

| 五个问题 | 回答 |
| --- | --- |
| 是什么 | 工具执行环境承载 Shell、文件、测试和浏览器等操作。 |
| 为什么需要 | 限定副作用范围、隔离用户任务、管理进程资源。 |
| 没有它 | 合法 Tool Call 仍可损坏宿主、泄露密钥或跨会话污染。 |
| 实际怎么做 | Workspace + Sandbox + 超时/资源控制 + 日志和产物管理。 |
| 处于哪里 | Tool Runtime 之下，与 Policy Engine 共同约束可执行能力。 |

## 实践验证

创建两个任务工作区，分别运行会产生 `output.json` 的测试；验证互不可见。再测试符号链接越界、超时子进程清理、网络断开、无写权限情况下的错误回传。实验应在安全的临时目录或容器里进行。

## 源码阅读索引

1. [Codex `tools/orchestrator.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/orchestrator.rs)：重点看 `run` 如何协调 Tool Runtime 与批准/隔离执行。这是工具调用与策略的交汇点，而不是某种 OS 沙箱全部逻辑所在的文件。
2. [Codex `codex-rs` 源码树](https://github.com/openai/codex/tree/main/codex-rs)：继续检索 `sandbox`、`exec` 等模块，区分策略选择与平台级强制执行。
3. [OpenHands 源码仓库](https://github.com/All-Hands-AI/OpenHands)：从 Runtime 与执行工具入口追踪命令如何进入独立执行环境。具体子目录随版本重构，建议通过仓库搜索定位而不是记死旧路径。

**关键取舍**：Sandbox 的性能、可复用性、文件同步成本、调试便利与安全隔离强度之间存在冲突。对不可信代码，不能用“Prompt 中写禁止删除文件”替代操作系统级控制。

**下一章的问题**：即使沙箱本身安全，某些真正影响用户或生产环境的操作是否应该先问人？
