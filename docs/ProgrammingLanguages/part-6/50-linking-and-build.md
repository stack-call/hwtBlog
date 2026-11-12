---
title: "第 50 章｜多个源码文件如何变成一个可运行程序？"
sidebar_position: 50
description: "从订单系统的一次具体失败出发，理解多个源码文件如何变成一个可运行程序？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/50-linking-and-build
---

# 第 50 章：多个源码文件如何变成一个可运行程序？

订单程序拆为 `pricing.c`、`checkout.c`、`main.c`。单个文件都能生成目标代码，但程序运行时怎么找到位于另一文件里的函数？

## 函数声明让编译器知道如何调用 price

函数声明让编译器知道如何调用 `price`，定义放在另一个翻译单元。这种拆分对于模块边界与增量构建很有用。

**教学实现：**

```c
/* pricing.c */
long price(long amount) { return amount * 90 / 100; }
/* main.c */
long price(long amount);
int main(void) { return price(100) != 90; }
```

## 编译器可以生成一个“将来要调用某个符号”的目标文件

编译器可以生成一个“将来要调用某个符号”的目标文件，却不能凭空补上函数实现。我们需要把多个文件中的符号引用与定义对齐。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```bash
# 只编译 main.c（仅定义被引用函数的声明）
# gcc main.c -o app
# 链接阶段可能报告 undefined reference to price
```

**因此需要的能力。** 引入目标文件、符号表和链接过程：将未解析的外部符号与实际定义关联，形成可运行的镜像或库。

## 把新机制落实成代码

**改进后的教学实现：**

```bash
# 分别编译（需把上节代码按注释写入两个文件）
gcc -c pricing.c -o pricing.o
gcc -c main.c -o main.o
# 链接
gcc main.o pricing.o -o app
./app
```

`-c` 输出目标文件，最后一步将目标文件连接起来。示例片段直接嵌在正文，实操时读者按注释拆文件即可，无需下载外部示例仓库。

## 编译 main.c 时记录对 price 的引用

编译 `main.c` 时记录对 `price` 的引用；编译 `pricing.c` 时提供 `price` 的定义；链接器匹配符号并处理重定位；装载器再把最终产物映射到进程地址空间。

## Go 以 package 与模块构建为主要组织单位

Go 以 package 与模块构建为主要组织单位，构建工具会处理依赖编译和链接步骤。

**Go 示例（教学代码，非所引用仓库的源码节选）：**

```go
package pricing
func Price(x int64) int64 { return x * 90 / 100 }
// 其他包通过导入路径访问已导出的 Price
```

Go 的 package 名称与大小写导出规则不同于 C 头文件符号协议；底层仍必须形成可执行产物或由运行环境装载相关代码。

## 为什么声明了函数，链接器还说找不到

C 中两个源文件分别编译很自然：一个实现订单求和，一个在主函数调用。若调用方只看见函数声明却没有把实现所在目标文件链接进去，就可能出现未定义引用错误。

```c
/* total.c */
long total(long a, long b) { return a + b; }

/* main.c */
#include <stdio.h>
long total(long, long);
int main(void) { printf("%ld
", total(100,250)); }
```

可以分别执行 `cc -c total.c`、`cc -c main.c`，再 `cc main.o total.o -o app`。若只链接 `main.o`，调用方具有声明却找不到定义。这不是语法分析失败，而是**跨编译单元符号解析**失败。

目标文件、重定位、动态库与加载器随后才有存在的必要。C++ 名字改编、Rust crate、Go package 和 Java class loading 都有各自规则，不能把所有平台的链接过程理解成同一个命令。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [GCC 选项文档：-c / linking](https://gcc.gnu.org/onlinedocs/)。

阅读 `gcc -c`、链接选项，使用 `nm` 查看目标文件的未定义和已定义符号。不要把头文件简单理解为“复制代码执行”；声明与定义职责不同。

## 实验：故意拼错 main.c 中的函数名

故意拼错 `main.c` 中的函数名，分别观察编译阶段和链接阶段何时失败；再恢复符号并运行。

**应当观察到（自行运行前属于预期）：** 声明正确但缺少定义时可在链接阶段报未定义符号；如果函数声明本身类型不匹配，编译阶段也可能报告问题。

## 边界：分离编译有利于模块化与增量构建

分离编译有利于模块化与增量构建，也带来 ABI、符号命名和依赖管理问题。动态链接和跨语言 FFI 还会放大这些边界。

**跨语言迁移时，** C/C++ 的目标文件与链接模型最直观；Rust、Go、Zig、Java、Python 的构建与装载过程不同，但都必须解决模块可见性与外部定义的关联。

**下一层问题：** 为什么读取文件不能只靠普通内存操作？

[上一章：为什么 Python、Java 与 C/Rust/Go 有不同的执行路线？](49-execution-routes.md) · [下一章：为什么读取文件不能只靠普通内存操作？](../part-7/51-file-io-and-syscalls.md)
