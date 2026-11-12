---
title: "第 43 章｜如何让一个极简解释器真正跑起来？"
sidebar_position: 43
description: "从订单系统的一次具体失败出发，理解如何让一个极简解释器真正跑起来？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/43-build-a-mini-interpreter
---

# 第 43 章：如何让一个极简解释器真正跑起来？

我们已经能把 `100 + 250` 解析成一棵树。接下来不依赖 Python 的 `eval`，能否自己按照树节点计算出结果？

## 解析后知道根是 BinOp

解析后知道根是 `BinOp`，左右是常量。仅有结构还不能执行；我们必须为每一种节点定义它如何产生值。

**教学实现：**

```python
import ast
node = ast.parse("100 + 250", mode="eval")
print(type(node.body).__name__)  # BinOp
```

## 树节点有不同种类

树节点有不同种类，单纯读取 `.value` 只能处理常量。我们需要根据节点结构递归计算子表达式。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
# 如果只支持 Constant，遇到 BinOp 就不知道如何求值
node = ast.parse("100 + 250", mode="eval").body
# node.value 不存在，不能当作常量处理
```

**因此需要的能力。** 为每一种暂时支持的 AST 节点定义解释规则：先解释子节点，再合成当前节点的值。由此得到最小树遍历解释器。

## 把新机制落实成代码

**改进后的教学实现：**

```python
import ast

def evaluate(node):
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return evaluate(node.left) + evaluate(node.right)
    raise ValueError(type(node).__name__)

root = ast.parse("100 + 250", mode="eval").body
print(evaluate(root))  # 350
```

解释器只支持整数常量与加法节点，已足够运行当前例子；其他节点明确拒绝而不是假装支持完整 Python。这里的 evaluate 是教学实现，不是 CPython 的求值器。

## 访问 BinOp

访问 BinOp：先递归解释左 Constant 得 100，再解释右 Constant 得 250，最后执行 `100+250` 返回 350。递归调用栈保存“返回父节点后还要做什么”。

## Java 的访问者模式也可对不同节点类型分派求值逻辑

Java 的访问者模式也可对不同节点类型分派求值逻辑。

**Java 示例（教学代码，非所引用仓库的源码节选）：**

```java
sealed interface Expr permits Num, Add {}
record Num(int value) implements Expr {}
record Add(Expr left, Expr right) implements Expr {}
// 按节点类型解释：Num -> value；Add -> eval(left)+eval(right)
```

Java 例子只声明教学 AST 形状，没有提供完整解释器实现；注意 ADT/visitor 设计与具体运行指令并非同层概念。

## 让自己的解释器只做一件事

先不用真实订单语言，给解释器一种极小指令：`PUSH 100`、`PUSH 250`、`ADD`。维护一个栈：

```python
def run(code):
    stack = []
    for op, arg in code:
        if op == "PUSH":
            stack.append(arg)
        elif op == "ADD":
            b, a = stack.pop(), stack.pop()
            stack.append(a + b)
    return stack[-1]

assert run([("PUSH", 100), ("PUSH", 250), ("ADD", None)]) == 350
```

状态从 `[]` 变成 `[100]`、`[100,250]`、`[350]`。每条指令都规定了前置条件与状态转移。这个解释器没有变量、循环、错误处理，也暂时不用；它已经证明了文本之外还存在一层可执行表示。

现在给它非法指令 `ADD`，空栈会抛异常。怎样检查操作数数量、如何报告源代码位置，就成为下一个问题。真实解释器有更多指令、优化和运行时对象管理；不能把这段教学代码冒充 CPython 或 JVM 实现。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [CPython 3.13.5 字节码定义入口](https://github.com/python/cpython/blob/v3.13.5/Python/bytecodes.c)。

现在只比较概念上“解释 AST”的办法与 CPython 采用的字节码执行路线：CPython 不需要对每个普通表达式都递归遍历源码 AST。下一章才研究字节码。

## 实验：为教学 evaluate 添加乘法 ast.Mul

为教学 `evaluate` 添加乘法 `ast.Mult`，先预测 `100 + 2 * 3`，再运行并观察 AST 的优先级结构。

**应当观察到（自行运行前属于预期）：** 在加法和乘法分支都定义后，结果为 106；如果不支持 `Mult`，会触发我们显式的 ValueError。

## 边界：递归 AST 解释器易理解、易扩展少量节点

递归 AST 解释器易理解、易扩展少量节点，但节点访问和递归分派有成本。真实语言实现会采用其他 IR、优化或虚拟机。

**跨语言迁移时，** 这个玩具解释器用 Python 写，但它同样可以用 C、Java、Rust、Go、Zig 或 C++ 实现；解释器的语言与被解释语言是不同概念。

**下一层问题：** 为什么不直接反复遍历语法树？

[上一章：为什么语法正确的程序仍然可能不合法？](42-parse-vs-semantics.md) · [下一章：为什么不直接反复遍历语法树？](44-from-tree-to-bytecode.md)
