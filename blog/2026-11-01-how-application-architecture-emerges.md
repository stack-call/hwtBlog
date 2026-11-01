---
slug: how-application-architecture-emerges
title: 一个应用的架构是怎样一步步长出来的？
description: 从一个最小任务管理程序出发，沿真实需求的变化，理解模块划分、职责分层、高内聚低耦合、依赖倒置与适配器模式为什么出现。
date: 2026-11-01
tags:
  - 软件架构
  - 设计原则
  - 设计模式
  - Python
---

# 一个应用的架构是怎样一步步长出来的？

> **本文不是先展示一套“标准架构”，再要求你记住它。** 我们只做一件事：从一个能创建、完成任务的小程序开始。每遇到一个真实的新问题，才增加解决它所必需的机制。最后再回头看，为什么会出现模块、分层、接口和设计模式。
>
> **代码说明**：本文使用自建的、可运行的 Python 教学项目，而非某个开源仓库的源码。文中的“阶段性教学化简”用于说明演化过程；标为“配套代码节选”的片段取自随文提供的 `task_architecture_demo.py`。配套程序仅依赖 Python 3.10+ 标准库，已实际执行。它验证的是这些设计机制的行为，不代表生产级存储和并发能力。

<!-- truncate -->

## 1. 先别设计架构：把任务创建出来

假设我们要开发一个任务管理应用。第一版只有两个操作：创建任务、完成任务。

此时，用一个列表和两个函数就够了：

```python
# 阶段性教学化简：最小内存实现
tasks = []

def add_task(title):
    task = {"id": len(tasks) + 1, "title": title, "done": False}
    tasks.append(task)
    return task

def complete_task(task_id):
    for task in tasks:
        if task["id"] == task_id:
            task["done"] = True
            return task
    raise KeyError("任务不存在")

created = add_task("写博客")
complete_task(created["id"])
print(tasks)
```

输出是：

```text
[{'id': 1, 'title': '写博客', 'done': True}]
```

对目前的需求，这个实现没有问题。它短、直接，而且每一步都容易理解。**简单设计不是坏设计**；如果没有新的限制，完全没必要立即拆出 `Controller`、`Service`、`Repository` 等一整套层次。

不过，用户很快提出一个小要求：任务标题不能是空白。

## 2. 为什么业务规则不能只写在页面里？

直觉上，我们可以在输入框提交之前做一次判断：

```python
# 阶段性教学化简
if user_input.strip():
    add_task(user_input)
```

只从这个页面操作，确实不会创建空标题任务。但应用随后增加了一个导入入口：它不经过页面，直接调用 `add_task("   ")`。

结果列表里出现了一个没有有效标题的任务。问题不在“页面校验写得不够多”，而在于：**业务规则被放到了一个并非所有调用路径都会经过的位置。**

因此最小的修改不是引入架构框架，而是把校验放进真正负责创建任务的函数：

```python
# 阶段性教学化简

def add_task(title):
    title = title.strip()
    if not title:
        raise ValueError("任务标题不能为空")
    task = {"id": len(tasks) + 1, "title": title, "done": False}
    tasks.append(task)
    return task
```

这样，无论页面、脚本还是导入功能调用它，规则都不会被绕过。

我们暂时不用急着给这个做法起名字，只需记住一个事实：**同一项业务约束，应该由真正拥有这项业务职责的地方保证。** 这已经是后来讨论“内聚”的起点。

接着，用户又提出一个更实际的要求：关闭应用后，任务不能丢失。

## 3. 数据要保存了，为什么不能直接写文件？

上一版的列表只存在内存里。进程退出后，任务就消失了。

最容易想到的方法是使用 JSON 文件：启动时读取文件，创建或完成任务后写回文件。

```python
# 阶段性教学化简：只展示与持久化相关的部分
import json
from pathlib import Path

DATA_FILE = Path("tasks.json")

def load_tasks():
    if not DATA_FILE.exists():
        return []
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))

def save_tasks(tasks):
    DATA_FILE.write_text(
        json.dumps(tasks, ensure_ascii=False), encoding="utf-8"
    )

tasks = load_tasks()
# 创建或完成任务后，再调用 save_tasks(tasks)
```

对于一个单用户、低频操作的小工具，这依然是合理方案。

但我们已经看到了新的责任：`add_task` 不只要处理创建规则，还必须记得调用保存；`complete_task` 修改完成状态之后也必须保存。如果以后还要支持删除任务，就又多了一个必须记住保存的位置。

这时可以先把文件读写收敛到 `load_tasks` / `save_tasks` 两个函数，而不是在每个业务函数里复制 JSON 细节。**这是一次小范围的职责分离，还不需要引入通用接口。**

问题尚未结束。产品现在增加了一个要求：已经完成的任务，不允许被重复完成。

## 4. 任务开始拥有自己的规则：把数据和行为放在一起

在字典版本里，任何地方都能写出：

```python
# 阶段性教学化简
task["done"] = True
```

于是页面可以修改、导入脚本可以修改、批量操作也可以修改。只要有一个入口忘记检查“是否已经完成”，规则就会不一致。

我们真正缺少的不是更多 `if`，而是一个稳定的位置来维护**“任务自身什么状态是合法的、允许进行哪些操作”**。

这时才有理由把任务从普通字典升级为一个对象。

```python
# 配套代码节选：Task
from dataclasses import dataclass

@dataclass
class Task:
    id: int
    title: str
    done: bool = False

    def __post_init__(self) -> None:
        self.title = self.title.strip()
        if not self.title:
            raise ValueError("任务标题不能为空")

    def complete(self) -> None:
        if self.done:
            raise ValueError("任务已经完成")
        self.done = True
```

现在创建任务时会校验标题，完成任务时会检查当前状态。调用者不需要在每一个入口复制这两条规则。

这就是**高内聚**的一种具体表现：与任务状态和行为密切相关的规则，被放在一起管理。

但注意，我们不是因为“领域模型”这个词很高级才创建 `Task` 类；是因为多个入口都需要遵守同一套任务规则，才有必要让规则集中。

新的问题出现了：我们准备为业务逻辑编写自动化测试。测试时不希望反复创建和删除真实的 `tasks.json`。未来应用也可能把任务放到云端，而不只是放在本地。

## 5. 当存储需要替换，依赖关系才成为问题

如果业务操作直接写死 `Path("tasks.json")`，测试就必须接触文件系统；切换云端存储时，创建和完成任务的代码也要一起修改。

在这里，业务真正关心的是什么？不是 JSON，也不是文件路径。它只关心两件事：

1. 获取已有任务。
2. 保存一项任务。

于是我们可以用一个最小契约描述这两件事：

```python
# 配套代码节选：TaskStore
from typing import Protocol

class TaskStore(Protocol):
    def list_all(self) -> list[Task]: ...
    def save(self, task: Task) -> None: ...
```

它不规定数据放在哪里。内存、JSON 文件、远端服务，只要能履行这两个操作，就可以成为存储实现。

应用流程于是变成：

```python
# 配套代码节选：TaskService
class TaskService:
    def __init__(self, store: TaskStore) -> None:
        self.store = store

    def create(self, title: str) -> Task:
        next_id = max((t.id for t in self.store.list_all()), default=0) + 1
        task = Task(id=next_id, title=title)
        self.store.save(task)
        return task

    def complete(self, task_id: int) -> Task:
        task = next((t for t in self.store.list_all() if t.id == task_id), None)
        if task is None:
            raise KeyError("任务不存在")
        task.complete()
        self.store.save(task)
        return task
```

这里有一个值得停下来观察的变化。

以前，创建任务的代码知道“任务保存在 JSON 文件中”；现在，`TaskService` 只知道调用 `TaskStore`。**业务流程依赖的是存储能力的契约，而不是某个具体技术实现。**

这叫做**依赖倒置**：让较稳定的业务策略不必直接绑定容易变化的底层细节。

为了测试，我们先实现一个内存版存储：

```python
# 配套代码节选：MemoryTaskStore
from dataclasses import replace

class MemoryTaskStore:
    def __init__(self) -> None:
        self._items: dict[int, Task] = {}

    def list_all(self) -> list[Task]:
        return [replace(task) for task in self._items.values()]

    def save(self, task: Task) -> None:
        self._items[task.id] = replace(task)
```

`replace` 在这里用于返回和保存任务副本，避免外部直接改变内存存储里的对象，绕过 `save` 操作。它并不意味着这已经具备数据库事务能力。

业务代码只需要：

```python
# 调用示例：配套代码可直接运行
service = TaskService(MemoryTaskStore())
task = service.create("写博客")
service.complete(task.id)
```

如果换成 `JsonTaskStore(path)`，`TaskService` 的创建与完成流程都不用修改。JSON 版本的完整实现收录在配套代码中。

我们此刻才有必要区分两类职责：

- `Task` 维护任务的业务规则。
- `TaskService` 组织“创建”“完成”这些应用操作。
- `TaskStore` 表达应用需要的数据访问能力。
- `MemoryTaskStore`、`JsonTaskStore` 负责具体保存方式。

你会发现，所谓**分层**并不是先画四个框；它是这些不同职责在解决问题时逐步分化出来的。

## 6. 第三方接口长得不一样：什么时候需要设计模式？

现在假设我们需要把任务保存到一个已有系统。这个系统的 API 没有 `list_all` 和 `save`，只有：

```python
# 旧接口的形状（本文配套程序用 LegacyTaskApi 模拟）
api.fetch_records()
api.put_record({
    "task_id": 1,
    "task_name": "写博客",
    "finished": False,
})
```

我们的业务代码使用 `Task(id, title, done)`；旧系统却使用 `task_id`、`task_name`、`finished`。

最直接的做法，是去 `TaskService` 里增加针对旧接口的分支。但这样一来，应用流程就要知道旧系统的字段命名。以后换一个外部服务，又得修改同一个业务类。

问题已经很具体：**我们需要在不改动业务契约的前提下，让一个接口形状不同的外部系统参与协作。**

这时，一个小型转换层就够了：

```python
# 配套代码节选：LegacyTaskApiAdapter
class LegacyTaskApiAdapter:
    def __init__(self, api: LegacyTaskApi) -> None:
        self.api = api

    def list_all(self) -> list[Task]:
        return [
            Task(id=int(row["task_id"]), title=str(row["task_name"]), done=bool(row["finished"]))
            for row in self.api.fetch_records()
        ]

    def save(self, task: Task) -> None:
        self.api.put_record({
            "task_id": task.id,
            "task_name": task.title,
            "finished": task.done,
        })
```

这就是**适配器模式（Adapter Pattern）**在这个例子里的用法：把一个已有接口转换成应用所期望的接口。

它没有创造新的业务规则，也没有让系统自动变得更聪明。它只是把旧系统的字段格式差异隔离在一个位置。

我们现在可以写：

```python
# 配套代码可直接运行
service = TaskService(LegacyTaskApiAdapter(LegacyTaskApi()))
created = service.create("写博客")
service.complete(created.id)
```

`TaskService` 完全不知道背后使用的是旧接口。

这也解释了**设计模式究竟考虑什么**：它通常不决定整个系统的业务边界，而是在某个具体的设计矛盾出现时，提供一种可以复用的局部协作方式。没有接口不兼容的问题，就没有必要为了“用上 Adapter”而增加一个类。

## 7. 回头看：架构图现在才有意义

我们最初只有两个函数和一个列表。现在，为了满足真实需求，程序中自然出现了不同职责：

```text
          页面 / CLI / 其他入口
                   │
                   ▼
            TaskService
          （应用流程编排）
              │       │
              ▼       ▼
            Task    TaskStore
         （业务规则） （存储契约）
                       ▲
             由以下实现满足
                       │
          ┌────────────┼──────────────┐
          │            │              │
    MemoryTaskStore JsonTaskStore LegacyTaskApiAdapter
       （测试）      （本地文件）      （旧系统）
```

这张图不是项目开工时凭空决定的，而是前面几次问题驱动的设计选择的总结。

如果把代码拆成真实目录，一个可能的组织方式是：

```text
app/
├── domain/
│   └── task.py                  # Task 与其业务规则
├── application/
│   ├── task_service.py          # 用例编排
│   └── ports.py                 # TaskStore 契约
├── infrastructure/
│   ├── memory_store.py          # 内存实现
│   ├── json_store.py            # 文件实现
│   └── legacy_api_adapter.py   # 外部系统适配
├── interfaces/
│   └── cli.py                   # 用户入口
└── main.py                      # 创建并连接各对象
```

目录不是价值本身。真正重要的是：业务规则集中在哪里，应用流程依赖什么，以及技术实现可以在哪里替换。

## 8. 高内聚、低耦合到底位于哪个层次？

到这里，这个问题就容易回答了。

**高内聚、低耦合不是某个特定的架构层，也不是一种设计模式。它们是贯穿不同设计粒度的原则，也是衡量设计质量的视角。**

在我们的例子中：

- `Task` 集中维护标题校验和完成状态规则，体现类内部的**高内聚**。
- `TaskService` 专注创建、完成这类应用流程，体现职责层面的**高内聚**。
- `TaskService` 不认识 JSON 文件格式或旧 API 字段，体现模块间的**低耦合**。
- `LegacyTaskApiAdapter` 把外部协议变化限制在一个边界内，也是在控制**耦合**。

“低内聚、高耦合”通常意味着相反的危险状态：一个模块夹杂多种无关职责，同时又依赖大量其他模块的内部细节。改变一处功能，往往会牵动很多地方。

但也不要把“低耦合”误解为“零依赖”。任务创建仍必须使用存储能力。我们只是让依赖通过稳定契约表达，使变化更容易被局部吸收。

## 9. 目标、原则、架构和设计模式是什么关系？

现在可以把几个经常混淆的概念放到各自的位置了：

| 概念 | 在这个案例里回答的问题 | 具体体现 |
| --- | --- | --- |
| **设计目标** | 希望应用具有什么质量？ | 可维护、可测试、能替换存储方式 |
| **设计原则** | 怎样判断设计是否合理？ | 高内聚、低耦合、职责清晰、依赖倒置 |
| **架构设计** | 应用由哪些部分构成，怎样协作？ | 任务规则、应用流程、存储契约、具体实现之间的关系 |
| **设计模式** | 某个反复出现的具体问题如何解决？ | 使用适配器模式对接旧 API |
| **代码实现** | 这些设计最终如何落地？ | `Task`、`TaskService`、`TaskStore` 等代码 |

它们并不是互相排斥的五套理论，而是在不同尺度上描述同一项工程活动。**架构负责整体组织，原则指导取舍，模式解决局部问题，代码负责落实。**

## 10. 实验：这些设计真的产生了什么效果？

配套文件 `task_architecture_demo.py` 包含 `Task`、`TaskService`、内存存储、JSON 文件存储及模拟的旧系统适配器，能在不安装第三方依赖的情况下运行：

```bash
python task_architecture_demo.py
```

下面是实际运行得到的输出：

```text
内存: Task(id=1, title='写博客', done=True)
校验: 任务标题不能为空
重复完成: 任务已经完成
重新加载后: Task(id=1, title='写博客', done=True)
旧接口适配: Task(id=1, title='写博客', done=True)
```

这些结果分别说明：

1. 同一条应用流程可以在内存存储中完成创建和状态更新。
2. 空标题被任务规则拒绝。
3. 重复完成被任务规则拒绝。
4. 新建一个 JSON 存储对象后，仍能读出之前保存的任务。
5. 在不修改 `TaskService` 的前提下，模拟旧接口也能完成同样的操作。

这里必须说明验证边界：这是单进程、单线程的教学演示。它**没有**证明 JSON 写入具备原子性，没有解决并发创建时 `max(id) + 1` 可能冲突的问题，也没有验证真实网络故障、数据库事务或外部接口的兼容性。只有遇到这些需求，才应该继续引入对应机制。

## 11. 下一步应该先设计什么，而不是先套什么架构？

如果你要从零开发一个应用，可以先问五个问题：

1. **现在最重要的用户任务是什么？** 先实现一条完整的正常路径。
2. **哪些业务规则必须始终成立？** 让规则由一致的责任主体维护。
3. **哪些变化已经真实存在？** 只有当数据源、入口或业务策略需要变化时，才引入相应边界。
4. **哪些部分在变化时不应该互相影响？** 据此划分模块并约束依赖。
5. **怎样证明设计确实解决了问题？** 用最小测试验证行为，而不是只检查目录结构。

对于小应用，列表加函数可能已经足够；对于需要多入口、多存储、多团队协作的应用，模块化和职责分层会逐渐体现价值。对于安全、性能、事务、一致性和故障恢复等更严格的要求，还需要额外的架构决策，但它们也应由明确的需求与风险驱动。

回望整个过程：

```text
两个函数 + 内存列表
    ↓ 标题可能为空
把校验放到创建行为内部
    ↓ 多个入口都在修改任务状态
让 Task 统一维护业务规则
    ↓ 应用重启后数据丢失
增加 JSON 读写
    ↓ 测试和新数据源要求替换存储
抽出 TaskStore 契约，隔离具体实现
    ↓ 旧系统接口与契约不一致
增加 Adapter 转换层
    ↓
自然形成模块、职责边界与依赖方向
```

**一个好的应用架构，不是从一张漂亮的架构图开始的，而是每当现实问题迫使我们增加复杂度时，只增加足以解决当前问题的最小机制。**

这才是“高内聚、低耦合”的实际意义：让相关规则待在一起，让变化尽量停留在它本该发生的位置。
