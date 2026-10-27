---
id: modern-llm-chapter-07-inference-engine
title: "第七章：从逐 Token 生成到推理引擎——怎样让大模型服务更多用户？"
sidebar_label: "第七章：推理引擎与服务优化"
sidebar_position: 8
description: 从单请求自回归生成出发，逐步推导 Continuous Batching、PagedAttention、FlashAttention、Prefix Caching、量化与投机解码，并对照 vLLM v0.11.0 的真实源码。
---

# 第七章：从逐 Token 生成到推理引擎——怎样让大模型服务更多用户？

上一章讨论了一个很有吸引力的方向：模型参数不变，也可以通过多次采样、验证与搜索，把更多计算用在困难问题上。

但是，如果这个模型变成了一个真正的在线服务，我们很快会遭遇另一种困难。

假设我们继续使用贯穿本系列的代码修复任务。此时，服务器上同时来了三个请求：

- 请求 A：阅读一个大型代码仓库，预计生成很长的修复建议。
- 请求 B：解释一条测试报错，只需要简短回答。
- 请求 C：检查一个补丁是否符合代码规范。

它们使用的是同一个模型，也共享一块有限的 GPU。模型会做推理，不意味着系统就能高效地处理这些请求。

我们已经知道如何构建 Transformer、如何降低注意力的缓存量、如何训练推理模型。**这一章的问题换成了：怎样组织模型的执行和显存，让同一套权重能够同时服务更多请求？**

这次先不要打开 vLLM 的庞大架构图。我们从最简单、确实可以工作的单请求生成开始，然后看它每次在哪里浪费资源。

## 一、从一次普通生成开始：模型计算实际上分为两个阶段

最简单的推理流程如下：

```text
用户输入 Prompt
       ↓
一次前向计算：处理整个 Prompt
       ↓
得到第一个生成 Token
       ↓
只输入新 Token + 历史 KV Cache
       ↓
得到下一个 Token
       ↓
继续循环，直到停止
```

这里自然出现两个阶段。

**Prefill（预填充）**：一次处理已有输入 Token，构建对应的 KV Cache，并计算用于生成第一个 Token 的 logits。

**Decode（解码）**：在已有 KV Cache 的基础上继续自回归生成。最常见的非投机解码每轮为每个请求推进一个新 Token。

下面是概念伪代码，目的是展示阶段边界，并不是 Hugging Face 或 vLLM 的真实实现：

```python
# 概念伪代码：仅用于说明 Prefill / Decode

kv_cache, logits = model.prefill(prompt_tokens)
next_token = sample(logits)

while next_token != EOS:
    emit(next_token)
    kv_cache, logits = model.decode(next_token, kv_cache)
    next_token = sample(logits)
```

`Prefill` 通常有大量 Prompt Token 可一起计算，容易形成较大的矩阵乘法；`Decode` 在常规自回归模式下每个请求每轮只有少量新 Token，需要反复读取权重和历史 KV。两者的 GPU 利用形态很不同。

> [!NOTE]
> “Prefill 更偏计算密集、Decode 更容易受内存带宽限制”是常见工作负载下的经验，不是绝对定律。模型规模、Batch Size、上下文长度和硬件都会改变瓶颈。

在只有一个用户时，上面这套设计足够用。真正的问题从第二个用户到来时开始。

## 二、先把请求放进 Batch：为什么固定 Batch 很快就不够？

直观做法是把多个请求合并到一次前向计算中：

```text
请求 A ─┐
请求 B ─┼──> Batch ──> 同一个模型 ──> 分别输出
请求 C ─┘
```

这能摊薄某些调度和设备执行开销，但请求并不会在同一时刻到达，也不会在同一时刻结束。

假设 GPU 同时最多处理两个请求，先采用最简单的 **固定 Batch**：已经组成的一组请求必须全部结束，才能加入下一组新请求。

如果 A 在时刻 0 到达，需要四轮；B 在时刻 1 到达，需要两轮；C 在时刻 2 到达，需要一轮，那么：

```text
固定 Batch（最多 2 个并发位置）

时间 0： A
时间 1： A         B 等待
时间 2： A         B、C 等待
时间 3： A
时间 4： B + C
时间 5： B

A 完成于时间 3；B 完成于时间 5；C 完成于时间 4
```

B 明明在 A 运行时就到达了，但 GPU 上还有空位，却不能加入当前 Batch。

于是我们真正缺少的不是“更大的 Batch”，而是**在每一轮计算后，都允许请求加入、完成和退出的调度方式**。

这就是 Continuous Batching（连续批处理）要解决的问题。

### 实验 1：用纯 Python 看见动态加入请求的效果

下面的程序不调用任何模型。它只验证调度规则，是一个**教学化简的离散时间模拟器**。

```python
from dataclasses import dataclass


@dataclass
class Request:
    name: str
    arrival: int
    steps: int
    done: int = 0


def simulate(continuous: bool):
    requests = [
        Request("A", arrival=0, steps=4),
        Request("B", arrival=1, steps=2),
        Request("C", arrival=2, steps=1),
    ]
    active = []
    trace = []
    finished = {}

    for tick in range(10):
        # 固定 Batch 只在整组结束时补充请求；连续批处理每轮补充。
        if continuous or not active:
            for req in requests:
                if len(active) == 2:
                    break
                if req.arrival <= tick and req.done == 0 and req not in active:
                    active.append(req)

        trace.append((tick, [r.name for r in active]))
        for req in active:
            req.done += 1
            if req.done == req.steps:
                finished[req.name] = tick

        active = [r for r in active if r.done < r.steps]
        if len(finished) == len(requests):
            break

    return trace, finished


for mode in (False, True):
    trace, finished = simulate(mode)
    print("continuous:", mode)
    print("trace:", trace)
    print("finished:", finished)
```

实际运行结果（摘取完成时刻）：

```text
continuous: False
finished: {'A': 3, 'C': 4, 'B': 5}

continuous: True
finished: {'B': 2, 'A': 3, 'C': 3}
```

连续批处理让 B 在时间 1、C 在有空位的时间 3 加入，从而比固定 Batch 更早完成。我们只比较**调度轮次**，不能把这个实验解释成 GPU 加速倍数，因为模拟器没有矩阵计算、带宽竞争和调度开销。

连续批处理解决了请求进入 Batch 的时间问题，但新问题也同时出现了：**不同请求的上下文长度不同，KV Cache 怎样分配，才能随时加入和释放？**

## 三、请求能够动态加入，但显存不能无限预留

在第三章我们已经知道：KV Cache 避免重复计算历史 Key/Value，但它会随请求数、上下文长度和模型层数增长。

如果给每个请求一开始就预留最大长度的连续显存，方法虽然简单，却浪费很大。

考虑三个请求：

```text
请求 A：预计最多 1000 Token，实际使用 100
请求 B：预计最多 1000 Token，实际使用 600
请求 C：预计最多 1000 Token，实际使用 250
```

如果粗暴地为每个请求直接预留 1000 个 Token 的 KV 空间，实际只使用 950，预留却是 3000。

更严重的是：随着 Token 不断生成，缓存要增长；请求一结束，显存又要回收。如果每个请求始终要求一段连续的大空间，就会引入复杂的空间分配问题。

操作系统处理类似问题时，并不要求进程的数据一定放在一段物理连续的内存中，而是使用“逻辑地址到物理页”的映射。

对应到推理引擎，我们可以先尝试一个最小思路：**把 KV Cache 切成固定大小的 Block，每个请求保存自己的 Block Table。**

```text
请求 A 的逻辑缓存： [Block 0] [Block 1] [Block 2]
                         |         |         |
                         v         v         v
物理缓存池：          [物理 7] [物理 2] [物理 9] ...

请求 B 的逻辑缓存： [Block 0] [Block 1]
                         |         |
                         v         v
物理缓存池：          [物理 4] [物理 1] ...
```

请求的逻辑顺序是连续的，物理块却可以分散摆放。

这个思想构成了 PagedAttention 和 vLLM KV 缓存管理的重要基础。不过这里必须注意：**PagedAttention 不等于把注意力变成稀疏注意力。** 它主要改变缓存的存储布局与访问方式；模型仍然可以对所有可见的历史 Token 进行注意力计算。

### 实验 2：实现最小的逻辑块到物理块映射

先只实现分配和释放，不提前引入 Prefix Caching 或复杂的驱逐策略。

```python
# 教学化简：不是 vLLM 的 BlockPool 源码

class TinyBlockPool:
    def __init__(self, num_blocks):
        self.free = list(range(num_blocks))
        self.refs = [0] * num_blocks
        self.tables = {}

    def append_block(self, request_id):
        if not self.free:
            raise RuntimeError("KV Cache blocks exhausted")
        block_id = self.free.pop(0)
        self.refs[block_id] = 1
        self.tables.setdefault(request_id, []).append(block_id)
        return block_id

    def release(self, request_id):
        for block_id in self.tables.pop(request_id, []):
            self.refs[block_id] -= 1
            if self.refs[block_id] == 0:
                self.free.append(block_id)


pool = TinyBlockPool(num_blocks=5)
pool.append_block("A")
pool.append_block("A")
pool.append_block("B")
print("tables:", pool.tables)
print("free before:", pool.free)
pool.release("A")
print("free after:", pool.free)
```

实际运行结果：

```text
tables: {'A': [0, 1], 'B': [2]}
free before: [3, 4]
free after: [3, 4, 0, 1]
```

这段代码只管理 Block ID，不分配真实 GPU Tensor，也没有实现每个 Block 中 Token 的槽位寻址。

假设 Block Size 为 `b`，请求已经使用 `L` 个 Token，则至少需要的块数为：

```text
blocks(L) = ceil(L / b)

最后一个块的未用槽位 < b
```

它不要求预留整个最大上下文，因此比固定预留上限灵活得多。不过仍有最后一个块的内部碎片、块元数据和间接寻址成本。

### 去真实源码里看这套管理关系

下面的定位使用 **vLLM v0.11.0**，不是随时变化的 GitHub `main` 分支。

源码入口：

- 仓库：[vllm-project/vllm](https://github.com/vllm-project/vllm/tree/v0.11.0)
- 调度器：[vllm/v1/core/sched/scheduler.py](https://github.com/vllm-project/vllm/blob/v0.11.0/vllm/v1/core/sched/scheduler.py)
- KV 管理器：[vllm/v1/core/kv_cache_manager.py](https://github.com/vllm-project/vllm/blob/v0.11.0/vllm/v1/core/kv_cache_manager.py)
- 物理块池：[vllm/v1/core/block_pool.py](https://github.com/vllm-project/vllm/blob/v0.11.0/vllm/v1/core/block_pool.py)

真实源码节选（只截取入口和核心动作）：

```python
# vLLM v0.11.0 · vllm/v1/core/block_pool.py

def get_new_blocks(self, num_blocks: int) -> list[KVCacheBlock]:
    ...
```

`get_new_blocks()` 从空闲块池取出需要的物理块，并处理引用计数和可能的缓存块驱逐。这里省略的部分并非无关紧要，而是属于下一层的缓存复用与管理细节。

我们可以再缩小一步，定位分配的关键动作（真实源码节选，省略类型标注）：

```python
ret = self.free_block_queue.popleft_n(num_blocks)
```

这一步回答的是“新 Block 从哪里来”：不是每次临时申请一段任意尺寸的 GPU 内存，而是从已经管理好的 Block 池取出块。真正的 KV Tensor 则由模型执行与缓存布局层使用这些 Block ID 寻址。

再看另外两个入口：

```python
# 真实源码接口 · vLLM v0.11.0

# Scheduler.schedule()：决定本轮调度哪些请求和多少 Token
# KVCacheManager.allocate_slots()：为对应 Token 预留 KV 槽位
# BlockPool.get_new_blocks()：提供实际的物理缓存块
```

实际调用并不是简单的“三个函数排队执行”。调度器会结合 KV 管理器的结果判断某个请求能否获得资源，随后由执行器完成模型前向计算。下一轮还要根据生成结果更新请求的完成状态。

因此，系统已经从一个简单循环长成了：

```text
等待中的请求 + 正在运行的请求
              ↓
       Scheduler.schedule()
              ↓
    决定本轮各请求的 Token 预算
              ↓
      KVCacheManager 分配槽位
              ↓
       Model Runner 执行模型
              ↓
     更新输出、请求状态和缓存
              ↓
           下一轮
```

还有一个重要细节：vLLM V1 调度器并不要求每轮只能是纯 Prefill 或纯 Decode。它围绕每个请求尚未完成计算的 Token 数量分配预算，因此能在相同框架里支持 Chunked Prefill、Prefix Caching 和 Speculative Decoding。

## 四、KV Cache 可以复用了：为什么相同 Prompt 还要算两遍？

现在服务器能够灵活存放请求的 KV。但我们的代码修复服务有一个常见场景：

```text
请求 A：系统提示词 + 项目规范 + 请修复 bug A
请求 B：系统提示词 + 项目规范 + 请修复 bug B
```

它们的公共前缀完全相同。如果请求 B 的公共前缀再次从头计算，仍然会浪费 Prefill 计算。

于是可在 Block 管理上增加一层：**Prefix Caching（前缀缓存）**。

其基本思路是，把已经计算过的完整 KV Block 和它所对应的 Token 前缀建立联系。后来的请求如果前缀一致，并且模型、缓存身份和相关条件也一致，就能复用这些 KV Block。

```text
系统提示词 | 项目规范 | 用户问题 A
     |         |
     +----+----+
          |
       共享 KV
          |
     +----+----+
     |         |
系统提示词 | 项目规范 | 用户问题 B
```

这里的“相同”不能只检查某个块局部 Token 是否相同。KV 表示取决于它之前的上下文，还可能依赖模型版本、LoRA、模态内容以及缓存隔离策略。因此工程实现通常基于整个前缀的身份构造哈希，并管理引用计数。

在 vLLM `v0.11.0` 中可以继续看 `KVCacheManager.get_computed_blocks()`；其任务是查询已经计算且可复用的完整前缀块。`BlockPool` 中块的 `ref_cnt` 则帮助管理共享块的生命周期。

> [!NOTE]
> 前缀缓存节省的是**相同前缀的重复 Prefill**。它不自动减少第一次请求的计算，也不能把“意思相近但 Token 不同”的两个前缀当成相同缓存。

## 五、缓存的显存管理解决了，但 Attention 计算本身仍可能低效

现在我们已经能把多请求交给 GPU，并高效管理 KV。但看回一个具体的注意力计算：

```text
S = Q · K^T / √d
P = softmax(S)
O = P · V
```

直观实现可能先写出完整的 `S` 和 `P`，再继续计算 `O`。如果序列长度为 `N`，这两个中间矩阵在标准全注意力中会随 `N × N` 增长。

GPU 的快慢不只取决于乘法次数，还取决于**数据在显存 HBM 与片上存储之间搬运多少次**。

这时出现 FlashAttention：保持标准 Softmax Attention 的数学目标不变，通过分块、算子融合与在线 Softmax，尽量避免把巨大的中间注意力矩阵写回 HBM。

它与第四章的稀疏注意力不是同一类设计：

| 技术 | 主要改变什么 | 是否需要改变标准 Full Attention 的可见关系 |
| --- | --- | --- |
| Sliding Window / Sparse Attention | 哪些 Key 可被访问 | 通常需要 |
| FlashAttention | 如何在 GPU 上计算 Attention | 不需要 |
| PagedAttention | KV 如何分块存储、寻址 | 不需要 |

这里先不研究 CUDA Kernel。我们用一个更小的问题理解 FlashAttention 为什么能够“分块算 Softmax”。

### 问题：Softmax 不是要先看到所有分数吗？

设有一行注意力分数，普通 Softmax 可以写成：

```text
m = max(scores)
weights_i = exp(scores_i - m) / Σ_j exp(scores_j - m)
output = Σ_i weights_i · value_i
```

如果无法同时持有所有分数，可以逐块处理，并维护三个中间量：

- `m`：迄今处理过的最大分数。
- `l`：以当前最大值为基准的指数和。
- `acc`：以同样基准累积的 Value 加权和。

处理新块时重新选择最大值，并把旧的 `l` 和 `acc` 乘以对应的缩放因子，便能继续合并。

```text
new_m = max(old_m, max(block_scores))
scale_old = exp(old_m - new_m)

new_l = old_l · scale_old
        + Σ exp(block_scores - new_m)

new_acc = old_acc · scale_old
          + Σ exp(block_scores - new_m) · block_values

output = new_acc / new_l
```

这说明我们不必一直保存整条 Softmax 分布，只需要保留适当的归一化统计量。

### 实验 3：在线 Softmax 与标准 Attention 数值一致

```python
# 教学化简：演示一条 Query 的分块在线 Softmax。
# 这不是 FlashAttention 的 GPU Kernel，不用于速度对比。

import math
import torch


torch.manual_seed(7)
q = torch.randn(8, dtype=torch.float64)
k = torch.randn(19, 8, dtype=torch.float64)
v = torch.randn(19, 8, dtype=torch.float64)

scores = (k @ q) / math.sqrt(q.numel())
reference = torch.softmax(scores, dim=0) @ v

m = -float("inf")
l = 0.0
acc = torch.zeros(8, dtype=torch.float64)

for start in range(0, len(k), 5):
    chunk_scores = scores[start:start + 5]
    chunk_values = v[start:start + 5]

    new_m = max(m, chunk_scores.max().item())
    old_scale = math.exp(m - new_m)
    exp_scores = torch.exp(chunk_scores - new_m)

    l = l * old_scale + exp_scores.sum().item()
    acc = acc * old_scale + (exp_scores[:, None] * chunk_values).sum(0)
    m = new_m

online = acc / l
print("max_error:", (reference - online).abs().max().item())
assert torch.allclose(reference, online, atol=1e-12, rtol=1e-12)
```

实际运行，最大绝对误差约为：

```text
max_error: 1.1102230246251565e-16
```

这个实验只证明数学上可以分块计算，并不意味着 Python 循环会更快。真正的 FlashAttention 性能来自 GPU Kernel 的并行化、融合、存储层级利用与分块策略。

真实论文可以看 [FlashAttention](https://arxiv.org/abs/2205.14135) 与 [FlashAttention-2](https://arxiv.org/abs/2307.08691)。后者进一步改进线程块间的并行与工作划分。

## 六、如果 Attention 更快了，为什么模型推理还是贵？

即使 Attention 很高效，Decode 阶段仍然需要多次读取模型权重，FFN、投影层和其他计算也不会凭空消失。

我们可以试着减少权重占用的字节数，例如使用较低精度的表示。

假设有 `P` 个权重：

```text
FP16 / BF16 权重理论裸数据量 ≈ 2 × P 字节
INT8 权重理论裸数据量      ≈ 1 × P 字节
INT4 权重理论裸数据量      ≈ 0.5 × P 字节
```

这些只是**裸权重的理论存储量**，不包括缩放因子、零点、对齐、临时缓冲、KV Cache 和其它开销，也不代表实际端到端显存一定按同一比例下降。

这就引出了 Quantization（量化）。

量化不是简单地“把浮点数强制转成整数”。通常还需要从浮点数到低精度整数的尺度映射，并在计算时选择适当的运算 Kernel。

### 实验 4：最小的对称 INT8 权重量化

```python
# 教学化简：逐 Tensor 对称量化 + 反量化

import torch

w = torch.tensor([-2.0, -0.8, 0.1, 0.7, 1.5], dtype=torch.float32)
scale = w.abs().max() / 127

q = torch.clamp(torch.round(w / scale), -127, 127).to(torch.int8)
restored = q.float() * scale

print("original:", w.tolist())
print("quantized:", q.tolist())
print("restored:", restored.tolist())
print("max_error:", (w - restored).abs().max().item())
```

实际运行的最大绝对误差：

```text
max_error: 0.007086575031280518
```

可以看到还原值与原始权重接近，但并不完全相同。低精度节省存储和访存时，可能引入额外的精度误差，而且某些硬件与 Kernel 对特定量化格式并不高效。

实际部署还会考虑：

- 权重量化与激活量化是否同时进行。
- 是否对不同通道或不同分组使用不同的缩放比例。
- 是否需要校准数据，以及是否进行量化感知训练。
- KV Cache 是否也可以降低精度。

因此，量化是**精度、显存、带宽、计算 Kernel 和模型质量的综合取舍**，不是任何场景下都能无条件加速。

## 七、一次只能生成一个 Token：能不能让大模型一次验证多个？

现在单次模型执行更高效了，但普通自回归解码仍然有一个根本特点：后一个 Token 要依赖前一个 Token，所以常规生成需要按顺序推进。

回到代码修复任务，假设一个较小的 Draft Model 能快速猜测大模型接下来可能输出的多个 Token：

```text
Draft 提议： 修复精度 → 跳过测试 → 提交
Target 认为： 修复精度 → 运行测试 → 提交
```

与其让大模型每次只生成一个 Token，可以先用便宜的机制生成一串草稿，再让大模型在一轮前向计算中检查这些 Token 在对应前缀条件下是否合理。

这就是 **Speculative Decoding（投机解码）** 的基本动机。

它的重点不是“让小模型替代大模型”，而是让较小开销产生的候选，尽可能被目标模型成批验证并接受。

### 实验 5：先看最容易理解的 Greedy 验证

```python
# 教学化简：只演示确定性贪心解码下的前缀验证。
# 不包含采样分布保持、KV 回滚与 GPU 并行执行。

next_by_prefix = {
    ("BOS",): "修复精度",
    ("BOS", "修复精度"): "运行测试",
    ("BOS", "修复精度", "运行测试"): "提交",
}


def target_next(prefix):
    return next_by_prefix[tuple(prefix)]


def verify_greedy(prefix, draft):
    accepted = []
    context = list(prefix)
    for token in draft:
        target_token = target_next(context)
        if token != target_token:
            return accepted, target_token
        accepted.append(token)
        context.append(token)
    return accepted, None


accepted, correction = verify_greedy(
    ["BOS"],
    ["修复精度", "跳过测试", "提交"],
)
print("accepted:", accepted)
print("correction:", correction)
assert accepted == ["修复精度"]
assert correction == "运行测试"
```

实际运行结果：

```text
accepted: ['修复精度']
correction: 运行测试
```

这个例子说明：一旦第二个草稿 Token 不符合 Target 的贪心预测，就不能继续直接接受第三个草稿 Token，因为第三个是在错误前缀下提议的。

真实算法更重要的性质是：在正确的接受与修正采样规则下，**可以保持目标模型的采样分布**。这与上面只比较字符串是否相同的简化版本不同。

请阅读论文 [Fast Inference from Transformers via Speculative Decoding](https://proceedings.mlr.press/v202/leviathan23a.html)。工程系统还要处理 Draft/Target 前向成本、接受率、批处理冲突、KV Cache 回滚等问题。

这也解释了为什么投机解码不一定总能加速：当草稿命中率低、草稿模型太贵或系统已高度饱和时，额外的工作可能抵消收益。

## 八、到这里，才值得看完整推理引擎的运行链

现在所有模块都已由问题引出，可以把它们合起来。

```text
用户请求
   |
   v
Request Queue
   |
   v
Scheduler  <---- 请求完成 / 新请求到达 / Token 预算
   |
   +---- Prefix Cache 查询：已有前缀能否复用？
   |
   +---- KV Cache Manager：本轮是否有足够的 KV 槽位？
   |
   v
Model Runner
   |
   +---- 高效 Attention Kernel（如 FlashAttention）
   +---- 量化或普通矩阵计算 Kernel
   +---- 可选：投机草稿与验证
   |
   v
采样、生成 Token、更新请求状态
   |
   +---- 完成：释放/保留可复用缓存
   |
   +---- 未完成：进入下一轮调度
```

注意，这张图主要是**控制流与资源调度关系**，不意味着 FlashAttention、量化、投机解码在 vLLM 中都必须通过同一个函数调用。不同后端可能有不同的执行路径。

### 真实源码精读：从调度器的一个方法入手

以 vLLM `v0.11.0` 为例，最值得优先读的入口是：

```python
# 真实源码签名 · vllm/v1/core/sched/scheduler.py

def schedule(self) -> SchedulerOutput:
    ...
```

它不是直接执行每层 Transformer，而是确定本轮哪些请求参与计算、为它们分配 Token 预算并协调缓存资源。

真实源码中，调度器先建立本轮预算（节选）：

```python
token_budget = self.max_num_scheduled_tokens
```

随后，对 RUNNING 请求根据尚未完成的 Token 数量计算待调度量，再限制到本轮预算内：

```python
num_new_tokens = min(num_new_tokens, token_budget)
```

这些变量建立了一个非常具体的约束：**即使某个请求还有大量 Token 未计算，也不能在这一轮无限占用执行资源。** 当它尝试调用 `KVCacheManager.allocate_slots()` 却拿不到足够缓存块时，调度器还可能触发抢占，让部分请求暂时退出执行。我们之前的最小调度器没有实现这个分支，因为那时只需要解释“请求怎样动态加入”，尚未遇到显存不足的问题。

这解释了为什么调度器不应该被写成“把所有请求串起来调用模型”——它必须先回答**本轮 GPU 能负担哪些请求、哪些请求还缺哪些 Token 的计算**。

当模型执行结束，另一个入口会消费本轮输出：

```python
# 真实源码签名 · vllm/v1/core/sched/scheduler.py

def update_from_output(self, scheduler_output, model_runner_output):
    ...
```

上面省略了参数类型注解和函数体，是**接口定位的教学节选**，不是可直接调用的替代实现。真实方法还处理停止条件、采样结果、请求状态更新和其他执行模式。

在 `update_from_output()` 中，输入包括本轮调度结果和 Model Runner 的输出；其中采样得到的 Token 会推动请求的输出状态变化。下一次 `schedule()` 看到的待计算 Token 数量也随之变化。这构成了完整的“分配预算 → 执行 → 反馈 → 再调度”循环。

沿源码阅读时，可以用下面这条调用关系建立地图：

```text
Scheduler.schedule()
       |
       | 决定请求和 Token 数量
       v
KVCacheManager.get_computed_blocks() / allocate_slots()
       |
       | 前缀复用与 KV 槽位安排
       v
BlockPool.get_new_blocks() / free_blocks()
       |
       | 物理 Block 生命周期
       v
模型执行与输出回传
       |
       v
Scheduler.update_from_output()
```

这是一张**概念层级的调用地图**，不表示所有请求都严格按此顺序逐个调用全部方法，例如缓存命中、资源不足或结束请求都有不同分支。具体执行仍以固定版本真实代码为准。

## 九、性能指标：怎样判断一种优化真的有效？

如果只说“推理速度提升”，很容易把不同指标混在一起。

| 指标 | 关注什么 | 常见误区 |
| --- | --- | --- |
| TTFT（Time to First Token） | 用户等到首个输出 Token 的时间 | Batch 更大未必让它变短 |
| TPOT（Time per Output Token） | 生成阶段每个输出 Token 的平均耗时 | 不应与总请求延迟混为一谈 |
| ITL（Inter-Token Latency） | 相邻输出 Token 之间的间隔 | 网络传输、流式缓冲也会影响 |
| Throughput（吞吐量） | 单位时间完成请求或输出 Token 数 | 吞吐提升可能以排队延迟为代价 |
| GPU Memory / KV Usage | 显存和缓存使用情况 | 权重显存与 KV 显存不是一回事 |
| Tail Latency（尾延迟） | 较慢请求的延迟分布，如 P95/P99 | 平均延迟好看不能代表所有用户 |

例如，Continuous Batching 可以提升很多服务场景的吞吐量，但它也可能让某个长 Prefill 任务影响已经在 Decode 的用户。因此工程上才会进一步出现 **Chunked Prefill**：把很长的 Prompt 处理拆成更小的计算片段，与其他请求的生成过程共同调度。

它不是改变模型的数学定义，而是把 **Prefill 的工作量拆分到多个调度步骤**。这也呼应了前面看到的 vLLM V1 Token 预算设计。

## 十、本章的边界：这些技术各自解决什么，没有解决什么？

| 技术 | 真正解决的问题 | 没有自动解决的问题 |
| --- | --- | --- |
| Continuous Batching | 动态接纳和释放请求、提高资源利用率 | KV 分配方式、长请求干扰 |
| PagedAttention | KV Cache 的块式存放和灵活寻址 | 标准 Full Attention 的全部计算 |
| Prefix Caching | 相同前缀的重复 Prefill | 不同前缀的通用语义复用 |
| FlashAttention | 减少 Attention 计算中的中间结果 IO | 权重体积、所有模型层的计算 |
| Quantization | 降低部分数据的存储和传输成本 | 量化误差及所有硬件上的加速 |
| Speculative Decoding | 利用低成本提议、成批验证加快生成 | 草稿接受率低时的额外开销 |
| Chunked Prefill | 更灵活地分配长 Prompt 计算预算 | 完全消除 Prefill 计算 |

这些技术可以组合，但组合不保证收益相加。比如增加 Batch Size 可能提高吞吐量，却让投机解码的收益下降；量化减小带宽压力，但如果 Kernel 不匹配，实测未必快。

现代推理引擎的设计目标因此不是“无条件启用所有优化”，而是根据任务和硬件选择合适的执行策略。

## 十一、回看本章：一个简单的生成循环怎样长成推理系统？

现在我们可以完整复述演化过程：

1. 一个用户时，用 `Prefill → Decode` 循环已经能回答问题。
2. 用户变多后，单请求执行让 GPU 资源没有充分利用，于是需要批处理。
3. 请求异步到达、长短不同，固定 Batch 会浪费空位，于是引入 Continuous Batching。
4. 动态 Batch 要求 KV Cache 动态增长、释放和共享，于是引入块式 KV 管理与 PagedAttention。
5. 多请求常有相同系统提示词和公共资料，于是引入 Prefix Caching。
6. 注意力内的中间矩阵带来大量存储 IO，于是采用 FlashAttention 一类的高效 Kernel。
7. 权重读取与模型存储仍然昂贵，于是考虑量化。
8. 逐 Token 生成的串行依赖仍然限制速度，于是探索投机解码。
9. 最后通过 TTFT、TPOT、吞吐量和尾延迟评估这些设计是否真正有效。

从这里也可以看到现代大模型技术的一个重要分界：

**模型能力决定它会生成什么；推理引擎决定它能以多大的成本、在什么延迟下，为多少用户生成。**

接下来的一章，我们将离开纯文本输入。新的问题是：**当输入变成图片、语音甚至视频时，怎样把这些不同模态连接到模型能够处理的表示空间中？**

---

## 本章源码与参考资料

以下链接用于固定源码定位和复核；本文所有完整可运行的小程序都属于教学化简，并不冒充生产代码。

1. [vLLM v0.11.0 源码标签](https://github.com/vllm-project/vllm/tree/v0.11.0)：固定版本。
2. [vLLM v0.11.0 Scheduler API 与源代码展示](https://docs.vllm.ai/en/v0.11.0/api/vllm/v1/core/sched/scheduler.html)：`schedule()`、`update_from_output()`。
3. [vLLM v0.11.0 KVCacheManager](https://docs.vllm.ai/en/v0.11.0/api/vllm/v1/core/kv_cache_manager.html)：`get_computed_blocks()`、`allocate_slots()`。
4. [vLLM v0.11.0 BlockPool](https://docs.vllm.ai/en/v0.11.0/api/vllm/v1/core/block_pool.html)：`get_new_blocks()`、`free_blocks()`。
5. [Efficient Memory Management for Large Language Model Serving with PagedAttention](https://arxiv.org/abs/2309.06180)：PagedAttention 和 vLLM 的原始论文。
6. [FlashAttention: Fast and Memory-Efficient Exact Attention with IO-Awareness](https://arxiv.org/abs/2205.14135)：IO-aware Attention 的基础。
7. [FlashAttention-2: Faster Attention with Better Parallelism and Work Partitioning](https://arxiv.org/abs/2307.08691)：进一步的并行与工作划分优化。
8. [Fast Inference from Transformers via Speculative Decoding](https://proceedings.mlr.press/v202/leviathan23a.html)：保持目标采样分布的投机解码。

> [!NOTE]
> 实验 1 和实验 2 是纯 Python 的调度、内存管理模拟。实验 3、4 需要 PyTorch。实验 5 是贪心投机验证的极简教学代码。它们验证具体机制，不是对 vLLM 或 GPU 性能的端到端基准测试。
