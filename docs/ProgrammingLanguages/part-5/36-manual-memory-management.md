---
title: "第 36 章｜为什么手工管理内存容易出错？"
sidebar_position: 36
description: "从订单系统的一次具体失败出发，理解为什么手工管理内存容易出错？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/36-manual-memory-management
---

# 第 36 章：为什么手工管理内存容易出错？

订单数据动态分配后，业务从一条路径扩展成“正常返回、参数错误、数据库失败”三条路径。某个失败分支忘记释放，长时间运行就会泄漏。

## 单条直线路径下申请、使用、释放很清楚

单条直线路径下申请、使用、释放很清楚。手工管理并不等于必然错误，只是每增加一个退出点就多一处需要遵守的约定。

**教学实现：**

```c
long *p = malloc(sizeof(long));
if (p != NULL) {
    *p = 100;
    free(p);
}
```

## some_error() 为真时

`some_error()` 为真时，指针丢失而动态内存尚未释放。调用次数增长会积累泄漏。若多释放一次或者释放后继续用，问题会变成更危险的无效访问。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```c
long *p = malloc(sizeof(long));
if (!p) return -1;
if (some_error()) return -2;  // 忘了 free(p)
free(p);
return 0;
```

**因此需要的能力。** 确保每条退出路径都遵守同一资源清理规则。最小修复可以是一处统一出口，而不是立即迁移整门语言。

## 把新机制落实成代码

**改进后的教学实现：**

```c
long *p = malloc(sizeof(long));
if (!p) return -1;
int result = 0;
if (some_error()) {
    result = -2;
    goto cleanup;
}
*p = 100;
cleanup:
free(p);
return result;
```

所有成功申请的路径都经过 `cleanup`，避免漏清理。这里 `goto` 不是为了随意跳转，而是 C 中一种清晰的集中资源清理写法；复杂资源还需要注意逆序释放。

## 正常路径设置结果后落到 cleanup

正常路径设置结果后落到 `cleanup`；异常路径先设置错误码再跳到同一标签；两者都调用 `free(p)`。这是“资源必须被线性清理”的设计直觉。

## C++ 可以用对象生命周期自动调用析构以降低漏清理风险

C++ 可以用对象生命周期自动调用析构以降低漏清理风险。

**C++ 示例（教学代码，非所引用仓库的源码节选）：**

```cpp
auto p = std::make_unique<long>(100);
if (some_error()) return -2;
return 0; // unique_ptr 离开作用域时自动管理资源
```

C++ 智能指针让释放与对象生命周期关联，不要求每个 return 手动 free。但循环引用、跨语言资源与不当所有权仍需处理。

## 谁释放资源？这是比 free 语法更重要的问题

假设 `load_orders()` 分配一块缓冲区，调用者用完后释放。这是可行契约，但如果错误路径提前 `return`，释放步骤就容易遗漏。

```c
char *buf = malloc(1024);
if (!buf) return -1;
if (read_failed) {
    free(buf);   /* 少写这一行会泄漏 */
    return -2;
}
free(buf);
```

若两个指针都认为自己负责释放，可能重复 `free`；若所有人都认为别人负责，就会泄漏；若释放后仍使用，就是悬垂访问。它们不是三个独立的“背诵错误”，而是**资源所有权的约定不清**在不同执行路径上的表现。

面对复杂函数，应该逐条列出所有提前返回、异常或错误分支，并标注每个资源在路径终点是否有明确清理责任。后续 C++ RAII 和 Rust Ownership 的价值，正是把这份责任从容易遗忘的手工约定推进成更系统的机制。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [C++ RAII 与 std::unique_ptr 参考](https://en.cppreference.com/w/)。

核对 `unique_ptr` 的析构和移动语义，比较 C 中各条 `goto cleanup` 路径。资源管理的核心是“清理责任唯一且稳定”，不是某个风格偏好。

## 实验：给 C 函数再增加一个 if (invalid_i

给 C 函数再增加一个 `if (invalid_input())` 退出点，验证所有在分配后的路径是否都到达 `free`；可用编译器 sanitizer 观察部分错误。

**应当观察到（自行运行前属于预期）：** 集中出口使新错误路径只需设置结果并跳转清理。内存检测工具能帮助发现问题，但无法证明所有路径一定安全。

## 边界：统一清理减少遗漏

统一清理减少遗漏，但遇到多个分配资源时仍需要管理顺序。手工管理不天然慢，风险来自复杂控制路径中的责任分散。

**跨语言迁移时，** C 统一 cleanup、C++ RAII、Rust Drop、Zig defer/errdefer、Go defer、Java try-with-resources、Python with 都试图让清理跟随更可靠的执行边界。

**下一层问题：** 为什么 C++ 使用 RAII？

[上一章：为什么动态数据需要不同的存储方式？](35-heap-allocation.md) · [下一章：为什么 C++ 使用 RAII？](37-raii-in-cpp.md)
