---
title: "异步与高并发 02：等待网络数据时，CPU 在做什么？"
sidebar_label: "二、等待数据时 CPU 在做什么"
sidebar_position: 3
description: "用可观察的进程与线程状态解释等待、唤醒和 CPU 调度。"
slug: /async-concurrency/02-scheduler
---

# 等待网络数据时，CPU 在做什么？

上一章的 `recv()` 要等一秒才返回。如果这段时间 CPU 也停止工作，那么任何网络延迟都会让整台机器停住。显然真实操作系统不会这样设计。

我们需要区分**正在执行**与**仍然存在、但暂时不能执行**这两件事。

## 先观察一个现象

让一个线程睡眠，另一个线程持续计数。`sleep` 只是为了构造可控制的等待；它不是网络 I/O，但能帮助我们看见 CPU 没有跟着睡眠线程停止。

```python title="实验 02｜一个线程等待，另一个线程做计算"
import threading
import time

stop = threading.Event()
counter = 0

def busy():
    global counter
    while not stop.is_set():
        counter += 1

t = threading.Thread(target=busy)
t.start()
print("main sleeping")
time.sleep(0.3)
stop.set()
t.join()
print("counter:", counter)
```

结果 `counter` 通常大于 0，说明主线程等待时，另一个线程得到了执行机会。但这个实验**不能证明两段 Python 字节码真正同时并行**；在标准 CPython 构建中，GIL 会影响同进程 CPU 密集型 Python 线程的并行执行。我们这里只需要证实：等待一个线程，不代表其他线程无法运行。

## Linux 发生了什么？

理解一条典型路径就够了：

```text
线程 T1 在用户态运行
    ↓
T1 进入 recv 系统调用
    ↓
内核发现尚无数据：T1 被安排等待某个条件
    ↓
调度器选择另一个可运行线程 T2
    ↓
T2 获得 CPU，继续工作
    ↓
网络数据到达，等待条件成立，T1 被唤醒
    ↓
T1 进入可运行状态；之后再由调度器安排执行
```

Linux 中实际存在 `TASK_RUNNING`、可中断/不可中断睡眠等多种状态；**线程“被唤醒”与“在 CPU 上运行”是不同事件**。具体状态取决于等待路径与内核实现，不要把所有阻塞调用都归为一个固定状态。

此时引入一个概念刚好必要：**上下文切换**。CPU 从 T1 转去运行 T2，需要保存和恢复足以继续各自执行的状态，包括架构相关的寄存器状态与调度信息。上下文切换不是免费的，但可以避免 CPU 无意义地等待外部设备。

## 为什么程序还会回到 `recv` 的下一行？

内核不是重新启动用户函数，而是在原来的系统调用完成后，使线程沿着保存的执行上下文返回用户态。前章看到的 `recv()` 返回值来自此次操作的结果。

这里有两个不同角色：

1. 网络协议栈等机制让等待条件变为满足，并触发相关唤醒。
2. 调度器选择**什么时候**让已可运行的线程获得 CPU。

不能把“网卡收到包”简单等同于“用户线程立刻运行”：中间可能有中断处理、软中断/其他内核路径、队列处理、唤醒和调度。网络收包的每个驱动细节不是本章所需的最小知识。

## 动手观察内核给出的状态

下面这个脚本会打印自己的 Linux 进程 ID，然后在 `recv` 等待。另开终端观察它的线程状态。此实验只适用于有 `/proc` 的 Linux。

```python title="实验 02B｜给 /proc 留一个观察窗口"
import os
import socket
import threading
import time

server = socket.socket()
server.bind(("127.0.0.1", 0))
server.listen()
port = server.getsockname()[1]

def delayed():
    conn, _ = server.accept()
    with conn:
        time.sleep(5)
        conn.sendall(b"done")
    server.close()

t = threading.Thread(target=delayed)
t.start()
print("PID:", os.getpid(), "port:", port, flush=True)
with socket.create_connection(("127.0.0.1", port)) as client:
    print("waiting in recv...", flush=True)
    print(client.recv(1024))
t.join()
```

观察命令：

```bash
ps -T -p <打印出来的PID> -o pid,tid,stat,comm
cat /proc/<打印出来的PID>/task/<某个TID>/status | grep '^State:'
```

`ps` 的 `S` 等状态显示的是采样时刻的状态，不代表从 `recv` 到返回期间状态从未变化。实验结束后 PID 会退出；把 5 秒改长可以扩大观察窗口。

## 历史旁注

1950–1960 年代的多道程序与分时系统，正是围绕“在等待与计算之间安排多个程序”展开。后来多处理器使多个执行实体还可能**真正同时执行**；但“能调度多个程序”与“有多个 CPU 核心”并不是同一个条件。

可进一步阅读 [Linux proc_pid_task(5)](https://man7.org/linux/man-pages/man5/proc_pid_task.5.html)、[sched(7)](https://man7.org/linux/man-pages/man7/sched.7.html)。这里讲的是已核实接口与机制的教学化简，不将示意箭头当成某个内核函数调用链。

**下一步的问题**：既然等待期间别的线程可以执行，那数据查询服务为什么不用第二个线程同时请求另一个上游？去[第 03 章](./03-threads.md)。
