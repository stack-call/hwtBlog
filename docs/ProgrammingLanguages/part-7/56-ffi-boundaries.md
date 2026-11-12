---
title: "第 56 章｜为什么与外部代码交互需要格外小心？"
sidebar_position: 56
description: "从订单系统的一次具体失败出发，理解为什么与外部代码交互需要格外小心？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/56-ffi-boundaries
---

# 第 56 章：为什么与外部代码交互需要格外小心？

订单核心用 Rust 写，老的税率库只有 C API。能否直接把 Rust 的字符串或数组传给它，让两个语言互相调用？

## 函数只有一个机器整数参数和整数返回值

函数只有一个机器整数参数和整数返回值，适合建立跨语言调用的最小示例。复杂对象、异常和生命周期先不出现。

**教学实现：**

```c
long tax_c(long amount) { return amount / 10; }
// 一个稳定且简单的 C 函数 API
```

## 不同语言对字符串、数组、对象的布局与资源所有权可能不同

不同语言对字符串、数组、对象的布局与资源所有权可能不同。二进制调用边界需要比语言级类型名更精确的协议。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```text
若把 Rust 的 String 直接当作 char* 传入，
不知道谁拥有内存、是否有 NUL 结尾、编码和长度是什么。
只共享“字符串”这个名称不足以互操作。
```

**因此需要的能力。** 在 FFI 边界使用明确 ABI、稳定布局、兼容类型及所有权约定。先用简单整数函数打通，再增加缓冲区与错误传递。

## 把新机制落实成代码

**改进后的教学实现：**

```rust
unsafe extern "C" {
    fn tax_c(amount: std::ffi::c_long) -> std::ffi::c_long;
}
// 调用示意：unsafe { tax_c(100) }
// 需要把对应 C 目标文件正确链接进最终产物
```

`extern "C"` 声明跨语言的调用约定，`c_long` 对齐目标平台的 C long。实际调用仍在 `unsafe` 边界，并要求链接到正确符号；这段声明不包含税率库本身。

## Rust 调用方准备 c_long 实参

Rust 调用方准备 `c_long` 实参，根据 C ABI 调用 `tax_c`；C 函数计算 10 并通过 ABI 返回；Rust 取得结果。若改成字符串，就必须额外约定长度、编码和释放责任。

## Python 可使用 ctypes 调用兼容 C ABI 的动态库

Python 可使用 ctypes 调用兼容 C ABI 的动态库，但要显式配置参数与返回类型。

**Python 示例（教学代码，非所引用仓库的源码节选）：**

```python
import ctypes
# lib = ctypes.CDLL("./libtax.so")  # 需要实际已构建的库
# lib.tax_c.argtypes = [ctypes.c_long]
# lib.tax_c.restype = ctypes.c_long
```

ctypes 可以帮助封装 ABI 调用，但错设类型可能导致不安全访问。没有编译库的情况下，本片段只展示 API 设置，不伪称已经运行。

## 把 C 指针交给另一门语言时发生了什么

Java、Python、Rust、Go 等语言都可能需要调用现有 C 库。假设 C 返回一个新分配的缓冲区及长度，调用方必须知道它能读多少字节、由谁释放、何时不再有效。

```c
/* 假设 C 库提供以下 API，教学签名 */
unsigned char *load_receipt(size_t *length);
void free_receipt(unsigned char *buffer);
```

即使函数签名写对了，另一门语言的 GC 也不会自动理解这个 C API 的分配契约。返回的是借用指针还是所有权？能否跨线程使用？错误是否通过空指针、错误码还是别的通道表达？这些都必须写在 FFI 边界约定里。

Rust 的 `unsafe`、Python C API、JNI、Go cgo 和 Zig 与 C 的互操作模型不同。`unsafe` 不是“关闭全部规则”，而是将某些编译器无法证明的安全义务交给开发者。

FFI 测试应覆盖空缓冲区、分配失败、重复释放、异常传播和资源所有者提前退出。这样的测试比只验证一次正常函数调用更能揭露跨语言边界的问题。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [Rust Reference：External blocks、FFI](https://doc.rust-lang.org/reference/)。

核对 Rust 外部块与 unsafe 的规则，结合目标平台 C ABI 查 `long` 类型宽度。对于复杂结构，优先研究明确 `repr(C)` 与资源销毁 API 的边界。

## 实验：编译一个导出 tax_c 的 C 目标库

编译一个导出 `tax_c` 的 C 目标库，再从 Rust 调用它；核对输出与目标平台整数宽度，不要先跨语言传 `String`。

**应当观察到（自行运行前属于预期）：** 在符号可链接且 ABI 匹配时能得到 10；若缺少符号会在链接或装载时失败。

## 边界：FFI 可复用成熟库

FFI 可复用成熟库，但降低了某些语言安全边界，调试和部署更复杂。跨语言接口越窄、数据形状越稳定，维护成本通常越低。

**跨语言迁移时，** Python ctypes/cffi、Java JNI/Panama、Rust FFI、Go cgo、C/C++ ABI、Zig extern 都需处理调用约定、布局、错误与所有权四件事。

**下一层问题：** 为什么顺序处理无法充分利用等待时间或多核？

[上一章：为什么同一程序能在不同机器上运行？](55-portable-binaries-and-abi.md) · [下一章：为什么顺序处理无法充分利用等待时间或多核？](../part-8/57-concurrency-motivation.md)
