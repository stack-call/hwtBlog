---
title: "第 37 章｜为什么 C++ 使用 RAII？"
sidebar_position: 37
description: "从订单系统的一次具体失败出发，理解为什么 C++ 使用 RAII？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/37-raii-in-cpp
---

# 第 37 章：为什么 C++ 使用 RAII？

订单处理同时打开文件、获取锁、分配缓冲区。即使中途异常，三种资源也都应该按适当顺序释放。为每条分支写手工清理成本太高。

## 只有正常路径时

只有正常路径时，这段代码是合法的资源管理写法。但无法保证所有中途退出都能走到 `fclose`。

**教学实现：**

```cpp
std::FILE* f = std::fopen("orders.txt", "r");
if (!f) return;
// ... 很多中途返回与异常路径
std::fclose(f);
```

## 业务逻辑增加一条提前返回

业务逻辑增加一条提前返回，资源清理就漏掉了。异常进一步让手工逐条处理更加脆弱。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```cpp
std::FILE* f = std::fopen("orders.txt", "r");
if (!f) return;
if (invalid_order()) return;  // 文件句柄未关闭
std::fclose(f);
```

**因此需要的能力。** 让资源拥有者的生命周期和清理动作绑定：对象被销毁时自动释放自己负责的资源。这就是资源获取即初始化（RAII）的关键思想。

## 把新机制落实成代码

**改进后的教学实现：**

```cpp
#include <fstream>
#include <stdexcept>
void process() {
    std::ifstream file("orders.txt");
    if (!file) throw std::runtime_error("open failed");
    if (invalid_order()) return;
    // file 的析构负责关闭文件资源
}
```

`std::ifstream` 对象管理文件资源，作用域结束或异常展开时调用析构。这里“析构”不是垃圾回收：对象的生命周期结束通常对应确定的清理时机。

## 进入函数构造文件对象

进入函数构造文件对象；若订单无效直接 return，局部 `file` 退出作用域并析构；若后续抛异常，正常的栈展开也会销毁已构造完成的局部资源对象。

## Python 的上下文管理器明确包住资源作用域

Python 的上下文管理器明确包住资源作用域。

**Python 示例（教学代码，非所引用仓库的源码节选）：**

```python
with open("orders.txt", encoding="utf-8") as file:
    for line in file:
        print(line.strip())
# 离开 with 会调用上下文管理器的退出逻辑
```

Python `with` 与 RAII 都让资源清理跟随明确作用域/控制边界，但 Python 对象析构时机一般不能视为等同于 C++ 的确定性局部析构。

## 先让异常打断手动清理

C++ 订单处理函数先打开文件，再解析数据。如果解析抛出异常，手动写在尾部的 `close` 可能执行不到。RAII 将资源生命周期绑定到对象生命周期：

```cpp
#include <fstream>
#include <stdexcept>
void write_receipt() {
    std::ofstream out("receipt.txt");
    if (!out) throw std::runtime_error("open failed");
    out << "350
";
} // out 析构时关闭文件
```

关键不在“类能自动关闭文件”这个表面现象，而是 `out` 的析构与作用域退出关联；即使通过异常离开作用域，正常栈展开也会析构已构造的局部对象。RAII 可用于锁、套接字与动态内存，不只用于内存释放。

但析构不意味着外部副作用自动回滚。文件已写入的部分字节不会因为抛异常就恢复原状；系统崩溃、进程被强制终止也不保证常规析构都运行。资源清理与事务原子性是两个层次。

与 Python 的 `with`、Java 的 try-with-resources、Go 的 `defer` 对比，分别找出清理动作**注册在何处**以及**遇到哪类退出路径会触发**。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [C++ RAII / object lifetime 参考](https://en.cppreference.com/w/)。

核对析构与异常展开规则，再读标准库资源包装类的文档。不要把 RAII 仅解释为“智能指针”，文件、锁和其他资源都可以采用同一设计。

## 实验：写一个打印构造、析构日志的 C++ 局部对象

写一个打印构造、析构日志的 C++ 局部对象，分别走正常返回与抛异常路径；观察析构顺序。

**应当观察到（自行运行前属于预期）：** 已构造对象在适当作用域退出时按规则析构；若用 `std::terminate` 等非正常终止路径，不应假定所有析构仍能完成。

## 边界：RAII 简化异常安全

RAII 简化异常安全，但资源所有权必须合理建模；错误的共享所有权或析构中抛异常都可能引出新问题。

**跨语言迁移时，** Rust Drop 与 RAII 相近但配合所有权和借用规则；Zig defer/errdefer、Go defer、Java try-with-resources、Python with 选择了不同的清理触发机制。

**下一层问题：** 为什么 Rust 需要所有权与借用？

[上一章：为什么手工管理内存容易出错？](36-manual-memory-management.md) · [下一章：为什么 Rust 需要所有权与借用？](38-rust-ownership-and-borrowing.md)
