---
title: "第 53 章｜为什么网络读写可能一直无法完成？"
sidebar_position: 53
description: "从订单系统的一次具体失败出发，理解为什么网络读写可能一直无法完成？，以及由此带来的语法、运行行为与设计取舍。"
slug: /programming-languages/53-network-io-blocks
---

# 第 53 章：为什么网络读写可能一直无法完成？

订单服务从网络请求付款状态。本地计算很快，但远程银行系统可能一直不响应；原来 `result = call()` 的直觉开始失效。

## 本地函数可以迅速返回一个确定值

本地函数可以迅速返回一个确定值，调用方只需等待计算。它暗含函数执行不会长期依赖外部系统的假设。

**教学实现：**

```python
def bank_status():
    return "paid"
print(bank_status())
```

## 网络环境可能断开、丢包、拥塞或对端不响应

网络环境可能断开、丢包、拥塞或对端不响应。调用者无法控制远端何时返回，所以“等到成功”不是可靠的等待策略。

**用于触发限制的代码／输入（实验场景，不伪称来自框架）：**

```python
import socket
# 连接外部服务时如果不设置超时，某些网络操作可能长期等待
# socket.create_connection(("example.invalid", 443))
```

**因此需要的能力。** 让网络 IO 有明确超时、错误报告和可中断等待。然后才能进一步讨论阻塞与非阻塞 IO，以及复用等待线程。

## 把新机制落实成代码

**改进后的教学实现：**

```python
import socket

def connect_bank(host: str, port: int):
    try:
        return socket.create_connection((host, port), timeout=2.0)
    except OSError as error:
        return None

# 真正使用时还需用 with/close 释放 socket
```

超时将“无限等待”收敛为可观察失败边界；示例吞掉具体错误仅为突出超时，生产程序应保留错误信息并分类处理。这里没有发起真实支付请求。

## 客户端请求连接

客户端请求连接，系统尝试建立网络连接；成功则返回 socket，失败或超时抛异常；调用者取得失败结果后可以明确选择取消、重试或报告用户。

## Go 常用 context.WithTimeout 给网络调用提供

Go 常用 `context.WithTimeout` 给网络调用提供截止时间。

**Go 示例（教学代码，非所引用仓库的源码节选）：**

```go
ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
defer cancel()
// 将 ctx 传给支持 context 的 HTTP 请求或数据库操作
```

创建 Context 本身不会魔法般取消任何操作；被调用 API 必须接收并遵守取消信号。与 Python socket timeout 处于相近需求层，但机制不同。

## 调用远端支付网关为何可能卡住

本地 `total = sum(items)` 的计算通常在程序自己控制下结束；网络支付必须等待另一个系统。对端可能慢、断开、重试、返回部分数据。同步阻塞读取在等待结果时可能占住当前执行线程。

```python
import socket
with socket.create_connection(('example.com', 80), timeout=3) as s:
    s.settimeout(3)
    s.sendall(b'HEAD / HTTP/1.0
Host: example.com

')
    first = s.recv(1024)
```

这是说明 API 形状的教学片段，不承诺外部网络可达，也不能把 `recv(1024)` 当成“一次收到了完整响应”。TCP 是字节流，读取可能返回部分字节；要靠应用层协议定义消息边界。

非阻塞 IO 与事件通知可以让同一线程在等待网络时处理其他任务，但需要额外保存每个请求的进度。异步不是“让网络变快”，而是更有效安排等待时间。

## 对照规范和真实实现

**源码/规范阅读路线（供读者核对，并非声称已经逐行验证）：** [Go 规范 + 标准库 context/net 参考](https://go.dev/ref/spec)。

查 `context`/`net` 的 API 行为，进一步到 Go 1.24.5 `src/net` 的截止时间处理路径。不要把模拟超时当成真实网络重试已经验证通过。

## 实验：本地启动一个只接受连接、延迟回复的测试服务器

本地启动一个只接受连接、延迟回复的测试服务器，再用不同 timeout 值观察调用耗时与异常；不要依赖公共站点。

**应当观察到（自行运行前属于预期）：** 设置较短超时的客户端可能先于服务端回复而终止等待；实际异常类型与阶段需要按具体 API 和平台记录。

## 边界：超时防止无限等待

超时防止无限等待，却会引入“远端可能已经执行了请求，但本地不知道结果”的难题；重试不等于一定安全，尤其涉及支付。

**跨语言迁移时，** Python socket/asyncio、Java NIO、Go net/context、Rust async 生态、C poll/epoll、C++ IO 库、Zig 网络 API 都要面对外部等待的不确定性。

**下一层问题：** 为什么外部操作需要明确处理失败？

[上一章：为什么运行中的程序需要进程？](52-processes.md) · [下一章：为什么外部操作需要明确处理失败？](54-error-handling-boundaries.md)
