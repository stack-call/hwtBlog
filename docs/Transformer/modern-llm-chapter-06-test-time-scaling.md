---
id: modern-llm-chapter-06-test-time-scaling
title: 第六章：从一次生成到推理时计算，为什么模型不变也能解出更难的问题？
sidebar_label: 第六章：Reasoning 与 Test-Time Scaling
sidebar_position: 7
description: 从一次失败的代码修复出发，逐步理解采样、多候选验证、Best-of-N、过程验证、搜索以及推理时计算的取舍，并对照 Transformers v4.57.1 的真实生成源码。
---

# 第六章：从一次生成到推理时计算，为什么模型不变也能解出更难的问题？

上一章，我们从预训练开始，逐步认识了 SFT、偏好优化和强化学习。

这些训练方法关注的是：如何改变模型的参数，让模型更有可能生成符合目标的答案。

但现在假设模型已经训练完成，所有参数都被冻结，遇到一道困难的题目时，我们还能做些什么？

最直观的答案是：让它再试一次。

这听起来甚至不像一项新技术。然而沿着这条思路追问下去，我们会遇到现代推理模型非常重要的一组问题：

- 多生成几次，为什么可能找到第一次没有生成出的正确答案？
- 生成了许多答案，我们怎么知道应该选择哪个？
- 只检查最终答案，为什么有时候仍然不够？
- 是把一个答案想得更长好，还是同时探索多条解题路线好？
- 为什么增加推理计算不一定持续提升效果？

这一章暂时不增加任何 Transformer Layer，也不重新训练权重。我们从一个已经训练完毕、只能逐 Token 生成文本的模型开始，在模型外部逐层增加机制。

## 一、继续使用同一个任务：修复退款计算

设想用户提出一个代码修复任务：

> 退款金额应当先扣除优惠券，并且不能出现负数。请修复退款函数，使它通过测试。

这里我们故意把业务缩小到一个函数。输入均为整数分，避免把本章变成浮点精度教程。

当前实现是：

```python
# 教学任务：待修复代码

def refund_cents(price_cents, coupon_cents):
    return price_cents
```

它完全忽略了优惠券。

正确需求很明确：

```text
退款额 = max(商品金额 - 优惠券金额, 0)
```

我们先准备三个候选修复。注意，这里不是三个真实模型的输出，而是为了验证推理策略而预先写好的候选函数。

```python
# 教学化简：三个候选修复

def patch_ignore(price_cents, coupon_cents):
    return price_cents


def patch_subtract(price_cents, coupon_cents):
    return price_cents - coupon_cents


def patch_clamp(price_cents, coupon_cents):
    return max(price_cents - coupon_cents, 0)
```

我们可以立即看出：

- `patch_ignore` 没有修复问题。
- `patch_subtract` 修复了大部分正常场景，但大额优惠券会导致负退款。
- `patch_clamp` 才满足完整需求。

问题是，模型在生成候选时，并不会天然知道我们认为哪一个正确。

如果只允许它生成一个版本，一旦选中前两个候选，任务就失败了。

## 二、为什么一次生成不够？先区分概率和正确性

语言模型每一步输出的是候选 Token 的分数，经由采样策略可以得到一个概率分布。

最简单的贪心生成，每次选择当前概率最大的 Token：

```text
next_token = argmax(probabilities)
```

这能产生一条确定的输出路径，但**局部最可能的下一个 Token，不保证整份程序具有正确行为**。

同样，整段文本的模型概率很高，也不意味着它能通过测试。语言模型分数衡量的是模型生成这段文本的倾向，不是程序规范的证明。

假设我们的模型对三个完整候选的概率是：

```text
patch_ignore     0.50
patch_subtract   0.30
patch_clamp      0.20
```

这些数字是人为设定的教学分布，并不是某个真实语言模型的测量结果。

如果总是选择概率最高的候选，我们会稳定选到错误版本。

但这个分布还隐藏着另一件事：**正确答案虽然不是最可能的，却仍然有机会被生成。**

于是我们不立刻改模型，而是尝试允许它多生成几份候选。

## 三、第一层扩展：从一次采样到多次采样

先用纯 Python 模拟一个冻结的候选生成器。它不是神经网络，也不会根据任务改变想法；它只复现“不同候选具有不同采样概率”这一机制。

```python
# 教学化简 · 实验 1：一个固定分布的候选生成器
import random

PATCHES = {
    "ignore": lambda price, coupon: price,
    "subtract": lambda price, coupon: price - coupon,
    "clamp": lambda price, coupon: max(price - coupon, 0),
}


def sample_candidates(n, seed=1):
    rng = random.Random(seed)
    names = list(PATCHES)
    weights = [0.50, 0.30, 0.20]
    return rng.choices(names, weights=weights, k=n)


print("一次生成:", sample_candidates(1))
print("八次生成:", sample_candidates(8))
```

这次固定随机种子的输出是：

```text
一次生成: ['ignore']
八次生成: ['ignore', 'clamp', 'subtract', 'ignore', 'ignore', 'ignore', 'subtract', 'subtract']
```

在这个例子里，参数根本没有变化，增加的只有采样次数。

如果单次生成正确候选的概率是 `p`，并暂时假设每次独立采样，那么 `N` 次中至少出现一次正确候选的概率是：

```text
P(至少出现一次正确答案) = 1 - (1 - p)^N
```

对这里的 `p = 0.2` 而言：

| 采样次数 N | 至少包含一个正确答案的理论概率 |
| ---: | ---: |
| 1 | 20.00% |
| 4 | 59.04% |
| 8 | 83.22% |
| 16 | 97.19% |

这个计算揭示了多次采样的潜力，也揭示了它的局限。

**表格统计的是候选集合中存在正确答案的概率，不是系统最终回答正确的概率。**

即使生成了一百个候选，其中包含正确答案，我们仍然需要知道该选哪一个。

而且真实模型的采样往往相关，成功概率也依赖题目难度和采样参数；上述公式只是独立同分布假设下的教学分析。

于是，下一层问题自然出现了：我们缺少的不是更多答案，而是一个选择答案的依据。

## 四、第二层扩展：引入验证器，才有 Best-of-N

对于代码修复，最朴素的验证器就是测试。

我们先定义一组可以检查候选行为的用例：

```python
# 教学化简 · 实验 2：结果验证器
TESTS = [
    (1000, 200, 800),
    (500, 500, 0),
    (1000, 1200, 0),
]


def verify_patch(patch, tests=TESTS):
    passed = 0
    for price, coupon, expected in tests:
        if patch(price, coupon) == expected:
            passed += 1
    return passed


for name, patch in PATCHES.items():
    print(name, verify_patch(patch), "/", len(TESTS))
```

现在，候选能通过多少个测试，是一个可观察的、与语言模型概率不同的信号。

于是我们得到一个非常简单的系统：

```text
同一个问题
    |
    +--> 候选 A --> 验证器 --> 分数
    |
    +--> 候选 B --> 验证器 --> 分数
    |
    +--> 候选 C --> 验证器 --> 分数
    |
    +--> ...
                |
           选择最高分
```

这种“生成 N 个完整候选，再根据评价信号选择最好一个”的方式，通常叫做 **Best-of-N（BoN）**。

最小实现甚至只有几行：

```python
# 教学化简 · 实验 3：Best-of-N

def best_of_n(names, verifier):
    return max(names, key=lambda name: verifier(PATCHES[name]))


names = sample_candidates(8, seed=1)
selected = best_of_n(names, verify_patch)

print("候选:", names)
print("选中:", selected)
print("通过用例:", verify_patch(PATCHES[selected]))
```

使用上面的固定随机种子，可以得到：

```text
候选: ['ignore', 'clamp', 'subtract', 'ignore', 'ignore', 'ignore', 'subtract', 'subtract']
选中: clamp
通过用例: 3
```

这里有三个角色：

1. `sample_candidates` 负责**提出可能的答案**。
2. `verify_patch` 负责**判断答案的质量**。
3. `best_of_n` 负责**根据分数选择候选**。

在真实代码 Agent 中，第二步可能是运行测试、静态分析、检查编译结果，或综合多个评价器。

但这也意味着：**Best-of-N 的效果很大程度上取决于验证器是不是可靠。**

### 结果通过测试，并不等于真正修好了

假设我们遗漏了第三条测试，只检查前两种输入：

```python
# 教学化简：不完整的公开测试
PUBLIC_TESTS = [
    (1000, 200, 800),
    (500, 500, 0),
]

print("subtract 公共测试:", verify_patch(PATCHES["subtract"], PUBLIC_TESTS))
print("clamp 公共测试:", verify_patch(PATCHES["clamp"], PUBLIC_TESTS))
```

两个候选都能通过公开测试。但只有 `clamp` 能通过优惠券超出商品金额这一边界条件。

这说明验证器不是天然可靠的裁判。它只验证了测试中覆盖的行为。

对于任意代码，有限测试一般不能证明程序完全正确。对于自然语言问题，甚至可能没有像单元测试这样明确的判定条件。

所以我们不能把“采样越多”简单等同于“答案一定越准确”。当评分规则本身不完善时，采样越多反而越可能找到利用评分漏洞的答案。

## 五、再进一步：为什么要区分结果验证和过程验证？

继续看我们的退款问题。

如果只看最终返回值，我们能够检查某几个输入是否正确；但对于更长的任务，错误可能产生得更早。

比如模型的修复过程包含三步：

```text
步骤 1：确定优惠券是否应该参与退款计算
步骤 2：计算扣券后的金额
步骤 3：处理金额小于零的边界条件
```

如果第一步就错误地认为“不需要扣券”，后面即使写出了漂亮的代码，也无法补救这条路线。

我们可以在最终测试之外，尝试对每一个中间步骤提供反馈。

由此出现两种不同的评价方式：

- **Outcome Reward Model（ORM）**：根据完整结果或最终答案评分。
- **Process Reward Model（PRM）**：对中间推理步骤提供评价信号。

在实际研究中，ORM、PRM 通常指训练出来的奖励或验证模型。本章使用的单元测试和规则函数属于**确定性验证器**，不是训练好的 ORM 或 PRM。我们借助它们建立直觉，但不能混为一谈。

### 用一个极小的过程评分例子理解

```python
# 教学化简：按步骤检查方案是否覆盖必要条件

def step_feedback(plan):
    checks = [
        ("subtract_coupon" in plan, "必须扣除优惠券"),
        ("clamp_zero" in plan, "必须禁止负退款"),
    ]
    return checks


bad_plan = ["subtract_coupon", "return_raw"]
good_plan = ["subtract_coupon", "clamp_zero"]

print("不完整方案:", step_feedback(bad_plan))
print("完整方案:", step_feedback(good_plan))
```

这样做的价值不是把中间步骤“写得更好看”，而是让系统能够更早发现某种错误，并据此保留或放弃一个候选路线。

但过程验证也有代价：中间步骤可能难以严格标注，一个步骤看起来合理不一定真的有效。过程评分器本身也可能判断错误。

我们现在已经具备了两个东西：一个生成器，以及一个能对中间状态评分的机制。

下一步就有理由不等到整份答案生成完毕，而是在推理途中选择路线。

## 六、第三层扩展：从“最后选答案”到“生成过程中搜索”

Best-of-N 的特点是：**每条候选路径生成结束后，才进行评分与选择**。

当一份答案很长时，这可能浪费计算。例如一条路线在第一步就走错，却仍然花费大量 Token 将错误答案写完。

于是可以改变计算分配方式：

```text
初始问题
    |
  展开几种第一步
    |
  评价当前步骤
    |
  保留少数有希望的路径
    |
  继续展开下一步
    |
  再评价、再筛选
    |
  最终得到完整答案
```

我们先用一个两步的迷你搜索实现这个过程。

```python
# 教学化简 · 实验 4：带步骤评分的两步搜索

OPTIONS = [
    ["subtract_coupon", "ignore_coupon"],
    ["clamp_zero", "return_raw"],
]


def evaluate_prefix(steps):
    score = 0
    if "subtract_coupon" in steps:
        score += 2
    if "ignore_coupon" in steps:
        score -= 2
    if "clamp_zero" in steps:
        score += 2
    if "return_raw" in steps:
        score -= 1
    return score


def search_with_verifier(width=2):
    beams = [([], 0)]
    for choices in OPTIONS:
        expanded = []
        for steps, _ in beams:
            for choice in choices:
                new_steps = steps + [choice]
                expanded.append((new_steps, evaluate_prefix(new_steps)))
        beams = sorted(expanded, key=lambda item: item[1], reverse=True)[:width]
    return beams


print("保留路径:", search_with_verifier(width=2))
```

这一教学评分规则下，运行得到：

```text
保留路径: [(['subtract_coupon', 'clamp_zero'], 4), (['subtract_coupon', 'return_raw'], 1)]
```

这里的 `width` 表示每轮保留多少条搜索路线。

当 `width = 1` 时，搜索空间很窄，早期一次判断失误可能让正确路线永久消失；当 `width` 增大时，能够保留更多可能性，但计算与存储也随之增长。

这与常见的 **Beam Search（束搜索）** 在“保留有限数量的部分路径”这一形式上类似，但有一个关键区别：

**普通语言模型 Beam Search 通常依照累计模型对数概率选择路径，并不会自动检查代码是否正确。**

上面的例子是“带外部过程评分器的教学搜索”，不能直接称为 Transformers 的标准 Beam Search。

实际研究还可能探索树搜索、回溯、选择性扩展和动态预算分配等策略。但对这一章来说，先理解“在最终完成之前评价中间路径”就够了。

## 七、回到真实源码：Transformers 里是如何生成和搜索的？

到这里，我们已经从问题中自然推导出三种方式：

1. 单次生成。
2. 多次生成，再由外部验证器选择。
3. 生成过程中保留多条候选路径。

现在去看一个真实项目：Hugging Face Transformers。

固定版本：`v4.57.1`。

源码入口：

- `src/transformers/generation/configuration_utils.py`：`GenerationConfig`。
- `src/transformers/generation/utils.py`：`GenerationMixin.generate`、`_sample`、`_beam_search`。

固定版本链接：

- [GenerationConfig 源码](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/generation/configuration_utils.py)
- [GenerationMixin 源码](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/generation/utils.py)

### 7.1 生成入口首先决定采用哪种策略

`generate()` 的关键职责之一，是读取生成配置、确定模式并调用相应方法。

```python
# 真实源码节选 · Transformers v4.57.1
# generation/utils.py · GenerationMixin.generate

generation_config, model_kwargs = self._prepare_generation_config(
    generation_config, use_model_defaults, **kwargs
)
generation_mode = generation_config.get_generation_mode(assistant_model)
if isinstance(custom_generate, Callable):
    decoding_method = custom_generate
else:
    decoding_method = getattr(type(self), GENERATION_MODES_MAPPING[generation_mode])
```

这段代码的调用者是外部的 `model.generate(...)`。

它接收用户的生成配置，确定当前是采样还是 Beam Search 等模式，拿到相应解码方法；后续由这个解码方法持续调用模型前向计算、选出 Token、更新输入和缓存。

其中模式映射包含下面几项：

```python
# 真实源码节选 · Transformers v4.57.1
# generation/utils.py · GENERATION_MODES_MAPPING（节选）

GENERATION_MODES_MAPPING = {
    GenerationMode.SAMPLE: "_sample",
    GenerationMode.GREEDY_SEARCH: "_sample",
    GenerationMode.BEAM_SEARCH: "_beam_search",
    GenerationMode.BEAM_SAMPLE: "_beam_search",
}
```

为什么贪心和采样都指向 `_sample`？因为二者的外层自回归循环相同，只是在选择下一个 Token 的方法上不同。

### 7.2 看 `_sample` 如何选 Token

```python
# 真实源码节选 · Transformers v4.57.1
# generation/utils.py · GenerationMixin._sample

if do_sample:
    probs = nn.functional.softmax(next_token_scores, dim=-1)
    next_tokens = torch.multinomial(probs, num_samples=1).squeeze(1)
else:
    next_tokens = torch.argmax(next_token_scores, dim=-1)
```

注意这里 `next_token_scores` 是已经处理过的 Token 分数，可能受到温度、Top-K、Top-P 等生成配置的影响。

- `do_sample=True`：根据当前概率分布随机抽取 Token。
- `do_sample=False`：选择最高分 Token。

选出来的 `next_tokens` 会追加进 `input_ids`，下一次循环继续生成。

因此，改变采样方式并没有改变模型参数；它改变的是如何从已有概率分布中挑选路径。

### 7.3 多返回几个序列，还不是 Best-of-N

`GenerationConfig` 包含：

```python
# 真实源码节选 · Transformers v4.57.1
# generation/configuration_utils.py · GenerationConfig.__init__

self.do_sample = kwargs.pop("do_sample", False)
self.num_beams = kwargs.pop("num_beams", 1)
self.num_return_sequences = kwargs.pop("num_return_sequences", 1)
```

我们可以在具有生成能力的 Transformers 模型上请求多个采样结果：

```python
# 接口示意：需要自行准备模型、分词器与模型权重
# 不是本章已经运行的实验

outputs = model.generate(
    **tokenizer(prompt, return_tensors="pt"),
    do_sample=True,
    num_beams=1,
    num_return_sequences=8,
    max_new_tokens=256,
)

candidates = tokenizer.batch_decode(outputs, skip_special_tokens=True)
```

这一步只是获取候选。它没有定义“程序修复成功”是什么，更不会自行执行测试。

要构成 Best-of-N，还必须由应用层补上：

```python
# 概念伪代码：外部验证环节，不是 Transformers 内置函数

candidates = generate_many(prompt, n=8)
scored = [(candidate, run_tests(candidate)) for candidate in candidates]
answer = max(scored, key=lambda item: item[1])[0]
```

这里的 `run_tests` 必须由应用实现，也可能需要隔离运行环境、执行超时、资源限制，以及避免执行不可信代码。

> [!NOTE]
> `num_return_sequences` 仅表示希望返回多少个序列。对于不同生成模式，参数有相应约束；例如 `num_beams=1` 且 `do_sample=False` 的普通贪心生成不支持请求多个不同序列。要得到多个随机候选，通常使用采样模式。

### 7.4 `_beam_search` 解决的又是另一件事

在 `GenerationMixin._beam_search` 中，框架会维护若干活跃候选，并按照累计分数选择下一轮继续扩展的路径。

固定版本里的辅助过程包含下面的关键分支：

```python
# 真实源码节选 · Transformers v4.57.1
# generation/utils.py · GenerationMixin._get_top_k_continuations（节选）

if do_sample:
    topk_indices = torch.multinomial(
        nn.functional.softmax(accumulated_log_probs, dim=-1), num_samples=beams_to_keep
    )
    topk_log_probs = torch.gather(input=accumulated_log_probs, dim=1, index=topk_indices)
else:
    topk_log_probs, topk_indices = torch.topk(accumulated_log_probs, k=beams_to_keep)
```

它所比较的 `accumulated_log_probs` 是候选路径累计的语言模型概率信息。

这可以让框架更系统地探索较高概率的文本序列，但不会自动变成“根据单元测试搜索正确程序”。

如果希望按照测试结果或步骤奖励分数选择路径，需要额外设计搜索逻辑与评分机制，而不是仅仅增大 `num_beams`。

## 八、为什么有的推理模型会生成更长的思考过程？

目前我们增加推理计算的方式，主要是并行或多次生成不同候选。

但还有另一条路线：**不增加完整候选的数量，而是在同一条生成轨迹中进行更长的中间推导**。

例如，一个模型面对程序修复问题，可以先在生成过程中分析：

```text
需求包含两个约束：
1. 扣掉优惠券。
2. 退款不能小于零。

先实现减法，再检查边界条件。
最终选择 max(price - coupon, 0)。
```

这类中间推导通常被称为 reasoning trace 或 reasoning tokens，具体呈现和训练形式因模型而异。

它可能让模型有机会在输出最终答案前分解问题、比较路线或修正前面的判断。

但请注意两个容易混淆的点。

**第一，要求模型“多想一会儿”，与训练得到更好的推理策略不是同一回事。**

如果模型没有学会如何利用额外 Token，单纯要求写得更长，可能只是生成更多重复、错误或貌似合理的解释。

**第二，生成的推理文本不天然等于模型真实而完整的内部因果过程。**

可见的解释有时能帮助检查逻辑，但它也可能省略关键影响因素，或者形成事后合理化。我们不能仅凭文字很长，就认定推理更正确。

2024 年公开介绍的 OpenAI o1 研究，展示了在强化学习训练的基础上，增加推理时计算与更高任务表现之间的关系。DeepSeek-R1 论文则展示了强化学习如何促进某些复杂推理行为的发展。这些是特定训练设置下的实证结果，不代表任何模型都能无限地靠增加生成长度提高准确率。

## 九、统一理解 Test-Time Scaling：到底在扩大什么？

现在我们可以给本章讨论的问题一个更精确的名称：**Test-Time Scaling（推理时计算扩展）**。

它指的是，在模型已经训练好之后，针对一个输入分配更多推理阶段计算，以争取更好的输出质量。

请区分三类计算：

| 计算发生在哪里 | 典型方式 | 是否更新参数 |
| --- | --- | --- |
| 预训练 / 后训练 | 继续训练、SFT、RL | 是 |
| 单条推理轨迹内部 | 产生更多中间推导 Token | 通常否 |
| 多条候选之间 | Best-of-N、验证器排序、搜索 | 通常否 |

前一章的 GRPO 是一种用于**训练策略参数**的方法；本章的 Best-of-N 是模型参数固定时的**推理选择**方法。

它们可能被组合起来：先训练出更善于解题的生成器，再在推理时对困难问题分配额外搜索预算。

### 计算成本不是抽象的“多想几秒”

假设生成 `N` 条完整候选，每条平均输出 `T` 个 Token，那么仅从输出 Token 数量看，其规模大致为：

```text
候选生成量 ≈ N × T
```

但实际延迟和算力消耗不与这个乘积严格线性对应。并行批处理、共享前缀计算、KV Cache、硬件利用率和验证器本身的成本，都会影响最终表现。

更长的单条推理也要支付额外的解码计算；如果模型对长推理能力不足，还可能增加错误累积。

因此，Test-Time Scaling 的真正问题不是“能不能多花计算”，而是：

**给定一份固定预算，我们应该把计算花在更长的单条推理、更多候选，还是更强的验证和搜索上？**

相关研究表明，在具体数学推理设定中，更有效的计算分配可以优于直接增加 Best-of-N 采样次数，而最优策略会随着题目难度变化。

## 十、为什么更多计算有时反而更差？

从前面的退款例子，我们已经得到一个关键反例：验证器如果遗漏边界条件，就会把错误答案误判为正确。

这类问题在复杂模型中会更严重。

设想某个评分器偏爱“解释很长、术语很多”的回答，而我们真正关心的是程序能否正确运行。

当只生成两份答案时，模型可能没有机会产生特别会讨好评分器的错误答案；当生成一百份时，这种答案出现的机会反而增大。

于是，模型从更多候选里选出的最高分答案，可能只是最擅长钻评分规则空子的答案，而不是真正正确的答案。

这是一种**奖励或验证器偏差被搜索放大**的现象。研究文献也提醒，使用不完美的评分模型做 Best-of-N，可能在采样次数增长时出现效果退化。

另一个问题是候选缺乏多样性。如果同一个模型总是重复一种错误推理路径，生成一百次，也未必能覆盖正确答案。

最后还有预算问题：对于一眼就能算出的简单问题，生成 64 个候选通常没有必要；对于极难的任务，盲目增加候选数量可能不如尝试不同的搜索或反馈方式。

所以现实系统更合理的目标，是根据任务难度、验证可靠性和剩余预算，动态决定在哪里增加计算。

## 十一、回看这一章：从一次生成如何一步步演化出来

我们没有从一张复杂的推理系统架构图开始，而是一次只解决一个已经出现的问题。

```text
一次生成
   |
   | 单条输出可能错误
   v
多次采样
   |
   | 正确答案可能出现，却不知道选哪个
   v
结果验证 + Best-of-N
   |
   | 完整生成后才筛选，可能浪费大量计算
   v
过程验证 + 分步搜索
   |
   | 需要在搜索宽度、验证准确性与成本间取舍
   v
自适应 Test-Time Scaling
   |
   | 如何让模型更擅长利用额外推理计算？
   v
与后训练共同设计的 Reasoning Model
```

到这里应该已经能自己解释几个常被混在一起的概念：

- **Sampling**：从模型概率分布中生成候选。
- **Best-of-N**：生成多个完整候选，依照外部评价选出一个。
- **Beam Search**：在生成途中保留多条部分路径；标准实现通常按模型分数筛选。
- **Verifier**：评价候选是否符合任务要求，可以是测试、规则或学习得到的评分器。
- **ORM / PRM**：分别强调最终结果或中间步骤的奖励 / 评价信号。
- **Reasoning Model**：经过相关训练后，更善于在复杂任务中利用推理步骤或额外计算的模型；它不等同于简单打开采样开关。
- **Test-Time Scaling**：在参数固定条件下，增加或重新分配推理阶段计算的一类方法。

最重要的结论是：

**现代推理能力并非只来自更大的 Transformer，也不是仅靠把一段答案写得更长。模型训练、候选生成、验证质量、搜索策略和计算预算共同决定最终效果。**

## 十二、下一章：同样的能力，怎样真正部署得起？

回头看，本章的很多办法都有一个共同代价：需要生成更多 Token、维护更多候选，或者执行更多验证。

当只有一名用户，这或许还能接受；当系统需要同时服务大量请求时，推理计算会直接变成 GPU 显存、延迟和服务吞吐的压力。

因此下一章，我们不再继续增加模型的“思考方式”，而是转向推理系统本身：

> 如果模型需要生成更多 Token、同时服务更多用户，如何让 GPU 不被 KV Cache、低效批处理与不必要的计算拖垮？

这将自然引出 Continuous Batching、PagedAttention、量化、FlashAttention 在推理端的作用，以及投机解码等推理加速技术。

---

## 本章实验与源码的边界

- `sample_candidates` 是**教学化简**：它用固定概率表模拟候选生成，不是实际 LLM，也无法证明某个模型具有推理能力。
- `verify_patch` 使用确定性断言检查预先给出的可信 Python 函数，不等于对任意生成代码安全地执行测试。
- `search_with_verifier` 是**教学化简**：它用于说明分步扩展和剪枝，不代表 Transformers 的 `_beam_search` 会使用业务验证器。
- 文中标记“真实源码节选”的片段核对自 Transformers `v4.57.1`；只摘录与当前推导有关的局部代码，不是可独立执行的完整模块。
- 需要预训练模型权重的 `model.generate(...)` 片段明确标记为**接口示意**；纯 Python 实验不依赖 API Key 或模型下载。
- 源码版本检查与教学实验是两件事；不能用教学实验通过来声称完整模型训练或真实推理基准已经验证。

## 延伸阅读与固定源码

1. [Transformers `v4.57.1` 生成实现](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/generation/utils.py)：`generate`、`_sample`、`_beam_search`，以及生成模式分派。
2. [Transformers `v4.57.1` 生成配置](https://github.com/huggingface/transformers/blob/v4.57.1/src/transformers/generation/configuration_utils.py)：采样、Beam Search 和多序列返回的配置。
3. [Let's Verify Step by Step（ICLR 2024）](https://proceedings.iclr.cc/paper_files/paper/2024/hash/aca97732e30bcf1303bc22ac3924fd16-Abstract-Conference.html)：结果监督与过程监督在数学推理验证中的比较。
4. [Learning to reason with LLMs（OpenAI，2024）](https://openai.com/index/learning-to-reason-with-llms/)：强化学习与推理时计算增长的实证研究介绍。
5. [DeepSeek-R1 Technical Report（2025）](https://arxiv.org/abs/2501.12948)：强化学习与推理能力发展的模型实例。
6. [Scaling LLM Test-Time Compute Optimally（ICLR 2025）](https://proceedings.iclr.cc/paper_files/paper/2025/hash/1b623663fd9b874366f3ce019fdfdd44-Abstract-Conference.html)：推理时计算分配与任务难度的关系。
7. [Is Best-of-N the Best of Them?（ICML 2025）](https://proceedings.mlr.press/v267/huang25c.html)：不完美奖励模型下，多候选选择可能放大的偏差。
