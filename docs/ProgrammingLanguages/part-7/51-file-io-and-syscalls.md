---
title: "第 51 章｜为什么读取文件不能只靠普通内存操作？"
sidebar_position: 51
description: "从订单系统的一次具体失败出发，理解为什么读取文件不能只靠普通内存操作？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/51-file-io-and-syscalls
---

# 第 51 章：为什么读取文件不能只靠普通内存操作？

订单金额不再写在源码里，而是保存在磁盘上的 `orders.txt`。程序已经会访问内存，为什么还需要专门的文件接口？

## 所有数据都已经在进程内存时

所有数据都已经在进程内存时，普通的变量和循环就能完成工作。它假设数据由程序本身初始化。

**教学实现：**

```python
amounts = [100, 250, 300]
print(sum(amounts))  # 650
```

## 文件属于操作系统管理的外部资源

文件属于操作系统管理的外部资源，进程不能假设其内容天然位于自己的地址空间。我们需要让运行环境提供受控的读取能力。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
# 订单数据存在文件中，程序启动时不具备文件内容
# amounts = ???
# 直接对内存变量求和并不会自动从磁盘加载数据
```

**因此需要的能力。** 通过文件 API 请求外部数据，再把字节解释成业务值。文件句柄、读取失败与编码处理都是跨越内存边界后自然出现的概念。

## 把新机制落实成代码

**改进后的教学实现：**

```python
from io import StringIO
source = StringIO("100\n250\n300\n")
with source as file:
    amounts = [int(line.strip()) for line in file]
print(sum(amounts))  # 650
```

这里用 `StringIO` 模拟文本文件，不依赖预先准备物理文件；真实文件换为 `open("orders.txt")`。解析步骤与读取步骤不同，字符串 `"250"` 要转成数值。

## 读取每行文本得到 `"100

读取每行文本得到 `"100
"` 等字符串，`strip` 去掉换行符，`int` 解析为整数，列表得到三条金额，最后 sum 得到 650。读取和转换各有独立失败点。

## C 使用标准库文件 API

C 使用标准库文件 API，后面通常由 C 库与操作系统服务完成实际 IO。

**C 示例（教学代码，非所引用仓库的源码节选）：**

```c
#include <stdio.h>
FILE *f = fopen("orders.txt", "r");
if (f == NULL) { perror("open"); return 1; }
long amount;
while (fscanf(f, "%ld", &amount) == 1) {
    /* 在这里处理 amount */
}
fclose(f);
```

C 里的 `FILE*` 是标准库流抽象，不应直接当成 OS 文件描述符；Python 的文件对象也多经过自己的缓冲与解码层。

## 读文件为什么不像读数组

订单列表在内存时，`orders[0]` 可以直接访问程序已有数据。读磁盘文件却需要请求操作系统参与，并面对权限、路径不存在、读取中断等情况。

```python
from pathlib import Path
path = Path('orders.txt')
try:
    content = path.read_text(encoding='utf-8')
except FileNotFoundError:
    content = ''
```

我们并没有在 Python 中自行驱动磁盘硬件；标准库与运行时最终通过操作系统接口完成文件操作。C 的 `open/read` 或标准 IO、Go 的 `os.ReadFile`、Rust 的 `std::fs::read_to_string` 在表层 API 上不同，但都必须处理“外部资源并不一定按我们的计划存在”。

读取文件还有时间问题：在两次读取之间内容可能变化，因此“打开前检查文件存在，然后读取”并不能保证下一刻读取必然成功。这也说明错误检查应围绕实际 IO 操作，而不是用提前的假设替代异常处理。

实验：尝试读一个不存在的路径和没有权限的路径，比较错误种类；再写一个空文件，确认“读取成功但内容为空”和“文件不存在”是两个不同结果。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [CPython v3.13.5 IO 标准库与 `Modules/_io/`](https://github.com/python/cpython/tree/v3.13.5)。

从 Python `_io` 模块相关源码入口查读文件和缓冲的层次；再用 `strace` 或平台工具观察真实文件读取可能触发的系统调用。教学模拟不代表已观察磁盘 IO。

## 实验：把 StringIO 中一行换为 "abc"

把 `StringIO` 中一行换为 `"abc"`，观察错误发生在读取还是解析；再用 try/except 提供带行号的报错。

**应当观察到（自行运行前属于预期）：** 文本读取成功，但 `int` 解析 `abc` 时失败。读取字节、解码文本与解析业务金额是不同职责。

## 边界：文件 API 使外部数据可用

文件 API 使外部数据可用，也引入失败、延迟、编码和资源释放。`with` 让关闭资源更可靠，但不保证写入内容已持久落盘。

**跨语言迁移时，** 七门语言都通过标准库与操作系统交互；C `stdio`、Java NIO、Rust std::fs、Go os、Zig std.fs、C++ fstream、Python io 的层次不同。

**下一层问题：** 为什么运行中的程序需要进程？

[上一章：多个源码文件如何变成一个可运行程序？](../part-6/50-linking-and-build.md) · [下一章：为什么运行中的程序需要进程？](52-processes.md)
