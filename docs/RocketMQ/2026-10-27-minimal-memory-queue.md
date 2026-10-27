---
slug: /message-queue/minimal-memory-queue
title: 从 Python 列表开始：写出第一版消息队列
description: 继续追踪订单 1001 的通知任务，用最简单的 Python 列表分开消息的产生与处理，并通过两次独立启动，观察内存队列为何会丢失未处理消息。
tags: [消息队列, Python, RocketMQ, 源码精读]
---

# 从 Python 列表开始：写出第一版消息队列

上一篇，我们遇到了一个很小、但很真实的问题：订单 `1001` 已经保存成功，通知服务却响应得很慢。如果下单接口必须等待通知发送完毕，用户就会一直看着页面转圈。

我们曾尝试把通知交给后台线程。这样确实可以尽快返回结果，但也暴露了一个限制：任务只存在于本进程中，进程退出后，尚未执行的任务可能再也找不回来。

先不急着解决“任务不能丢”的问题。要理解究竟丢了什么，我们需要让这件事**可以看见**。

这一篇只做一件事：**把一次通知调用拆成“先留下任务”和“稍后处理任务”两个步骤。**

## 先把通知任务放到一个列表里

假设订单已由 `save_order()` 保存。我们原来的下单逻辑类似这样：

```python
# 教学化简：省略订单数据库和通知服务的真实实现。
def place_order(order_id):
    save_order(order_id)
    send_notification(order_id)
    return {"status": "success", "orderId": order_id}
```

现在不立即通知，而是先留下一份可以稍后读取的数据：

```python
# 教学化简：只保留第一版消息队列的核心。
pending_messages = []


def place_order(order_id):
    save_order(order_id)
    message = {"messageId": f"msg-{order_id}", "orderId": order_id}
    pending_messages.append(message)
    return {"status": "success", "orderId": order_id}
```

这里只有一个真正的新动作：

```python
pending_messages.append(message)
```

过去，`place_order()` 需要亲自完成通知；现在，它只负责留下“订单 `1001` 需要通知”的记录，然后返回。

此时列表里的内容是：

```python
[
    {"messageId": "msg-1001", "orderId": 1001}
]
```

这里的 `messageId` 只是我们的教学字段，不是 RocketMQ 的消息 ID 格式要求。引入它是为了让我们始终能够指认同一条消息，而不是把“订单”“任务”和“调用”混为一谈。

但记录留在列表里，并不会自己变成短信。还得有人把它取出来。

<!-- truncate -->

## 谁有空，谁就取出一条任务

我们先写一个最简单的处理函数：

```python
# 教学化简：延续上面的 pending_messages。
def consume_once():
    if not pending_messages:
        return

    message = pending_messages.pop(0)
    send_notification(message)
```

这里最值得关注的不是函数名，而是两个动作的顺序：

```text
pending_messages.append(message)   # 从尾部放入
pending_messages.pop(0)            # 从头部取出
```

对于一个只有单个处理者、且不会并发修改列表的例子，这意味着先放入的任务会先被取出。

于是，订单 `1001` 的通知过程终于能分成两段：

```text
第一段：产生任务                  第二段：处理任务

place_order(1001)                  consume_once()
       |                                  |
       v                                  v
  save_order(1001)                 从列表头部取出消息
       |                                  |
       v                                  v
  append(msg-1001)  --->  列表  --->  send_notification()
       |
       v
  返回下单成功
```

**重要的是：这还不是一个自动运行的后台消费者。** 目前只是两个可以在不同时间调用的函数，二者仍在同一个 Python 进程里。

但这个小模型已经足够说明一件事：下单函数返回时，通知不一定已经执行。我们终于把“提出任务”和“完成任务”分开了。

## 把它变成一个真正可以运行的实验

为了把结果看得更清楚，我们给这个最小模型补上两个细节：

- 用一个本地文本文件模拟“已经保存的订单记录”，让它在程序重启之后仍能被看到。这不是数据库，也不具备真实数据库的事务保证。
- 用 `print()` 模拟通知动作，只检查任务有没有被取出处理，不调用真实的短信或邮件服务。

下面是完整的教学程序。将它保存为 `minimal_memory_queue.py`，只需要 Python 3 即可运行，不需要安装 RocketMQ，也不需要 API Key。

```python title="minimal_memory_queue.py"
"""教学化简：用 Python 列表观察一条订单消息的生命过程。"""

import sys
from pathlib import Path

# 重新启动进程时，这个列表会重新初始化为空。
pending_messages = []

# 订单文件只用于模拟“已保存的订单记录”。
ORDER_FILE = Path(__file__).with_name("order-record.txt")


def save_order(order_id):
    ORDER_FILE.write_text(str(order_id), encoding="utf-8")
    print(f"订单已保存: {order_id}")


def place_order(order_id):
    save_order(order_id)
    message = {
        "messageId": f"msg-{order_id}",
        "type": "OrderCreated",
        "orderId": order_id,
    }
    pending_messages.append(message)
    print(f"消息已入队: {message['messageId']}")
    return {"status": "success", "orderId": order_id}


def send_notification(message):
    # 只是打印，未调用真实短信或邮件服务。
    print(f"通知已处理: 订单 {message['orderId']} 创建成功")


def consume_once():
    if not pending_messages:
        print("队列为空，没有任务可处理")
        return

    message = pending_messages.pop(0)
    send_notification(message)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if mode == "demo":
        result = place_order(1001)
        print(f"下单函数返回: {result}")
        print(f"消费前队列长度: {len(pending_messages)}")
        consume_once()
        print(f"消费后队列长度: {len(pending_messages)}")

    elif mode == "produce":
        result = place_order(1001)
        print(f"下单函数返回: {result}")
        print(f"进程结束前队列长度: {len(pending_messages)}")
        # 故意不调用 consume_once；进程结束后内存队列消失。

    elif mode == "inspect":
        if ORDER_FILE.exists():
            print(f"磁盘订单记录: {ORDER_FILE.read_text(encoding='utf-8')}")
        else:
            print("磁盘订单记录: 不存在")
        print(f"新进程队列长度: {len(pending_messages)}")

    else:
        raise SystemExit("用法: python minimal_memory_queue.py [demo|produce|inspect]")


if __name__ == "__main__":
    main()
```

注意这个实验暂时只处理订单 `1001`，文件会被后续运行覆盖。这是为了减少不相关的状态管理代码，而不是推荐在实际业务里这样保存订单。

### 实验一：先下单，后处理通知

在脚本所在目录执行：

```bash
python minimal_memory_queue.py demo
```

运行结果：

```text
订单已保存: 1001
消息已入队: msg-1001
下单函数返回: {'status': 'success', 'orderId': 1001}
消费前队列长度: 1
通知已处理: 订单 1001 创建成功
消费后队列长度: 0
```

在 `place_order()` 返回的时候，列表里仍有一条消息。直到我们手动调用 `consume_once()`，才执行模拟通知。

这验证了本章唯一想先实现的能力：**提交任务和执行任务可以发生在两个不同的步骤里。**

但它没有验证“后台自动执行”，更没有验证“消息可靠送达”。我们还没实现这些能力。

## 第二次启动程序：消息去哪儿了？

现在让问题暴露出来。这一次，我们只下单，不消费：

```bash
python minimal_memory_queue.py produce
```

输出：

```text
订单已保存: 1001
消息已入队: msg-1001
下单函数返回: {'status': 'success', 'orderId': 1001}
进程结束前队列长度: 1
```

程序退出前，列表里确实有一条未处理消息。

接下来，在同一目录执行另一次命令。这会启动**一个全新的 Python 进程**：

```bash
python minimal_memory_queue.py inspect
```

输出：

```text
磁盘订单记录: 1001
新进程队列长度: 0
```

现在出现了一个非常具体的矛盾。

订单记录还在，因为刚才我们把它写进了本地文件；但待处理消息不见了，因为 `pending_messages` 只存在于上一个进程的内存里。新进程启动时，代码又执行了一次 `pending_messages = []`。

注意，这甚至不需要制造真正的程序崩溃。**普通的进程退出并重新启动，就足以让未处理的内存消息消失。** 如果是意外崩溃，问题只会更难控制。

同样也不要由这个实验推断“只要把数据写到文件里，就已经具备了生产级可靠性”。这里的订单文件只用于观察正常的跨进程保留行为，并没有测试断电、文件损坏或数据库事务。

## 现在我们究竟缺少什么？

回到订单 `1001` 的执行过程：

```text
第一个进程

订单记录写到文件
        |
        v
msg-1001 放进内存列表
        |
        v
下单函数返回成功
        |
        v
进程退出
        |
        x  内存列表消失

第二个进程

订单文件还能读到
        |
        v
待处理消息列表却是空的
```

这一刻，我们不需要急着引入 Broker、集群、副本或者消费者组。

我们真正缺少的能力只有一个：

> **一条尚未处理的消息，不能只依赖创建它的进程继续存活。**

最小的改进方向也很明确：我们需要让消息本身保存在进程之外，使程序重新启动后仍有办法找到它。

接下来才值得研究如何保存消息、什么时候认为保存成功、消费者如何再次读取。它们会慢慢带我们走向真实消息系统的存储机制。

不过，列表中还有另外一个尚未解决的问题。现在的代码在执行通知之前就调用了 `pop(0)`：如果取出之后通知失败，消息也已经从列表里消失了。这涉及“处理成功”和“删除消息”之间的关系，我们先把它记下来，留到之后真正讨论消费失败时再解决。

## 回看这一小步

我们并没有造出 RocketMQ，也没有造出可以独立部署的消息服务。

但现在，读者已经可以亲自说清楚三件事：

1. `append()` 让下单函数只负责留下消息，而不必立即执行通知。
2. `pop(0)` 让另一个处理步骤取出消息；通知什么时候执行，由调用处理函数的时机决定。
3. 内存列表依附于进程，程序结束后，尚未处理的消息不会自动恢复。

下一篇，我们就沿着这个失败现场继续：

**《进程重启之后，消息还能找回来吗？》**

先尝试最直接的持久化方法，再观察它为了确保消息能够被恢复，究竟需要满足什么条件。
