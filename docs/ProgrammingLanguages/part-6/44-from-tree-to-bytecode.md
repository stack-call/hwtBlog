---
title: "第 44 章｜为什么不直接反复遍历语法树？"
sidebar_position: 44
description: "从订单系统的一次具体失败出发，理解为什么不直接反复遍历语法树？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/44-from-tree-to-bytecode
---

# 第 44 章：为什么不直接反复遍历语法树？

玩具解释器每执行一次加法都要检查 AST 节点类型、递归进入左右子树。订单循环跑百万次后，重复解释树结构有不必要的开销。

## 树遍历解释器在教学和低频规则引擎里足够好

树遍历解释器在教学和低频规则引擎里足够好，调试直观。但重复执行代码时，节点对象访问与递归调度可能成为成本。

**教学实现：**

```python
# 教学解释器递归结构
# eval(BinOp(Add, Constant(100), Constant(250)))
# 每次都进行节点类型判断和子树递归
```

## 语法结构基本不变

语法结构基本不变，我们却在执行阶段不断重复“这是加法节点”一类解析后的判断。现在需要一个更紧凑的执行表示。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
# 同一表达式执行 n 次
# for _ in range(1_000_000): evaluate(ast_node)
# 每次都重新遍历相同语法结构
```

**因此需要的能力。** 把树提前转换成一串操作指令，让小型虚拟机顺序取指执行。例如“加载 100、加载 250、相加、返回”，这就是字节码路线的直觉。

## 把新机制落实成代码

**改进后的教学实现：**

```python
def execute(code):
    stack = []
    for op, arg in code:
        if op == "PUSH": stack.append(arg)
        elif op == "ADD":
            right = stack.pop(); left = stack.pop()
            stack.append(left + right)
        elif op == "RETURN": return stack.pop()

print(execute([("PUSH", 100), ("PUSH", 250), ("ADD", None), ("RETURN", None)]))
```

指令序列比不断识别 AST 结点更直接，栈保存中间值。它是我们自己定义的教学 VM 指令，不是 CPython 真实字节码名或布局。

## 初始空栈；PUSH100 后 [100]；PUSH

初始空栈；PUSH100 后 `[100]`；PUSH250 后 `[100,250]`；ADD 弹出两个数再压入 350；RETURN 弹出并返回 350。执行行为与树解释器一致。

### 对照字节码读取状态的真实实现

字节码必须能取到此前赋给局部变量的值。CPython v3.13.5 的 [`Python/bytecodes.c`](https://github.com/python/cpython/blob/v3.13.5/Python/bytecodes.c) 包含：

**真实源码节选 · CPython v3.13.5 · `Python/bytecodes.c`：**

```c
replicate(8) pure inst(LOAD_FAST, (-- value)) {
    value = GETLOCAL(oparg);
    assert(value != NULL);
    Py_INCREF(value);
}
```

这里的 `GETLOCAL` 根据指令参数从局部槽位取得对象，`Py_INCREF` 维护 CPython 的引用计数。它支持本章“虚拟机按指令操作状态”的推论；却不能说明其他实现（例如 PyPy）必须采用相同存储方式。

## CPython 确实使用字节码执行路线

CPython 确实使用字节码执行路线，真实指令需用当前版本 `dis` 查看。

**Python 示例（教学代码，非所引用仓库的源码节选）：**

```python
import dis
def add(a, b):
    return a + b
dis.dis(add)
```

不同版本的 CPython 字节码与特化策略会变化；不可将我们的 `PUSH/ADD` 教学指令冒充真实 `BINARY_OP` 编码。

## 为什么提前转换成指令流

每次执行 `total + amount`，如果重新从源文本识别标识符与运算符，重复工作很多。把已经解析的结构转换成字节码，可以让执行器直接取操作码并执行约定动作。上一章 `PUSH` 与 `ADD` 其实已经是抽象指令。

Python 中可用标准库观察实际字节码：

```python
import dis

def add(a, b):
    return a + b

dis.dis(add)
```

具体指令名称会随 CPython 版本变化。例如 CPython 3.13 的 `BINARY_OP` 可以承担不同二元运算的通用路径，并可能进入更专门的优化路径。源代码没有变，内部执行形式可以变。

字节码虚拟机也不是只能解释：JVM 可把热点方法 JIT 编译成机器码；某些语言可以先把源码编译到其他形式再由运行时执行。因此“解释型语言”和“编译型语言”并非严格互斥的两种物种。

实验记录字节码的操作数加载、计算、返回几个阶段，而不是把每个指令名字当作稳定 API。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [CPython 3.13.5 `Python/bytecodes.c`](https://github.com/python/cpython/blob/v3.13.5/Python/bytecodes.c)。

在固定版本中搜索 `BINARY_OP`、`RETURN_VALUE` 等指令定义；对照 `dis.dis` 观察函数体怎样被表示。先理解输入栈、执行动作与输出栈，不必一次性阅读解释器生成器。

## 实验：给教学 VM 增加 MULTIPLY

给教学 VM 增加 `MULTIPLY`，使它能执行 100 + 2×3；手工列出栈变化，再执行核对。

**应当观察到（自行运行前属于预期）：** 指令顺序可为 PUSH100、PUSH2、PUSH3、MUL、ADD、RETURN，返回 106。栈中间状态能直观说明指令次序。

## 边界：字节码减少部分运行期解析成本

字节码减少部分运行期解析成本，但仍有指令分派开销；真实 VM 还要处理异常、调用、对象和优化，不能凭教学代码估算性能。

**跨语言迁移时，** Python/Java 常见虚拟机或字节码路径，C/Rust/Go/Zig 通常提前编译成本机代码；执行中间表示与语言是否静态类型是不同维度。

**下一层问题：** 为什么编译器需要中间表示？

[上一章：如何让一个极简解释器真正跑起来？](43-build-a-mini-interpreter.md) · [下一章：为什么编译器需要中间表示？](45-intermediate-representation.md)
