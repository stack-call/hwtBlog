---
slug: /message-queue/rocketmq-nameserver-route-refresh
title: 新 Master 已经产生，生产者为什么还在向旧地址发送？
sidebar_position: 17
description: 沿订单 2002 的主从故障切换，从旧路由缓存导致写入失败出发，推导 Broker 注册、NameServer 路由与客户端定时刷新，精读 RocketMQ 5.5.1 对应源码。
tags: [消息队列, RocketMQ, 源码精读, NameServer, 路由发现, 故障切换]
---

# 新 Master 已经产生，生产者为什么还在向旧地址发送？

上一章，订单 `2002` 的恢复消息 `replay:msg-2002` 已经复制到副本 B。Controller 确认 A 不再适合继续担任 Master，于是选出 B，并推动 Broker 角色切换。

但业务没有立刻恢复。监控显示，生产者还在连接旧 Master A 的地址。

```text
Controller:   当前 Master = B
生产者缓存:    broker-a 的 Master = A:10911
消息发送:      connect A:10911 → 失败
```

这不是“选主失败”，而是我们在前一章没有回答的另一个问题：**谁负责告诉生产者，新 Master 已经换了位置？通知是否会瞬间到达所有客户端？**

我们继续只追踪同一笔订单。已有消息是 `replay:msg-2002`，新要发送的事件记作 `replay:msg-2002-next`。先从最小实现开始，直到确实需要新的机制再引入它们。

<!-- truncate -->

## Master 已经切换，旧地址为什么还会被使用？

最简单的生产者，可以在启动时获取一次 Broker 地址，然后一直使用它：

```python
# 概念伪代码，不是 RocketMQ 源码
broker_address = "10.0.0.1:10911"

def send(message):
    return send_to(broker_address, message)
```

A 正常运行时，这当然够用。地址已经找到，直接发送最省事。

但上一章的故障切换使这一行代码失效：

```text
切换前： A 是 Master ──生产者发 A──▶ 成功
切换后： B 是 Master ──生产者仍发 A──▶ 失败
```

即使 B 保存了全部消息，生产者也**不可能从一份旧地址字符串里推理出 B 在哪里**。它需要一份可更新的目录。

在介绍任何组件之前，先说清这个目录至少要回答的一个问题：对于 Topic `OrderRecovery`，`broker-a` 这个 Broker 组当前的可写地址是什么？

我们先设想它保存的不是物理机 IP 的单值，而是一组带角色标识的地址：

```text
Topic: OrderRecovery
BrokerName: broker-a
    brokerId=0 → 10.0.0.1:10911  （当前主）
    brokerId=1 → 10.0.0.2:10911  （当前备）
```

这里的 `brokerId=0` 是 RocketMQ 经典 Master 地址表里使用的约定。更完整的 Topic 路由不只包含这一张地址表，还有读写队列数量和权限等信息；为了抓住当前问题，我们先只看“Master 地址如何变化”。

## 目录从哪里得到正确地址？先让 Broker 主动注册

如果把目录写死在配置文件里，主从切换后仍要依赖人工修改。于是新需求很明确：**完成角色变化的 Broker 必须让路由目录知道自己的新身份。**

在本章固定的 RocketMQ 5.5.1 经典 Java 客户端链路中，提供这份路由目录的服务叫 **NameServer**。它不保存 CommitLog 消息正文，也不替 Controller 做选主，而是保存和提供 Broker 与 Topic 的路由信息。

先看写入端。Broker 在向 NameServer 注册时，需要附上自己所在的集群、BrokerName、BrokerId、地址和 Topic 配置。

**真实源码节选**，固定版本：`efa1be59d6fc747ad220f036c86030f05f0419f9`。

文件：[`broker/.../out/BrokerOuterAPI.java`，`registerBrokerAll()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/broker/src/main/java/org/apache/rocketmq/broker/out/BrokerOuterAPI.java#L3016-L3095)。这是 Broker 向多个 NameServer 发送注册请求的客户端侧入口；下面省略了请求体构造、并发发送和超时控制：

```java
final RegisterBrokerRequestHeader requestHeader = new RegisterBrokerRequestHeader();
requestHeader.setBrokerAddr(brokerAddr);
requestHeader.setBrokerId(brokerId);
requestHeader.setBrokerName(brokerName);
requestHeader.setClusterName(clusterName);
```

- **调用者**：Broker 注册流程，通过 Broker 外联接口构造远端请求。
- **输入**：当前 Broker 的地址、ID、名称和所属集群，而不是生产者随便指定的路由。
- **动作**：把 Broker 当前对外声明的身份写进注册请求头，连同 Topic 配置发送出去。
- **下游**：NameServer 接收注册信息并更新内存路由表。此处还没有对业务消息做任何写入。

更值得看的，是 NameServer 在角色变化后如何维护地址表。

**真实源码节选**，文件：[`namesrv/.../routeinfo/RouteInfoManager.java`，`registerBroker()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/namesrv/src/main/java/org/apache/rocketmq/namesrv/routeinfo/RouteInfoManager.java#L2569-L2650)：

```java
Map<Long, String> brokerAddrsMap = brokerData.getBrokerAddrs();
brokerAddrsMap.entrySet().removeIf(item -> null != brokerAddr && brokerAddr.equals(item.getValue()) && brokerId != item.getKey());
```

为什么要有第二行？因为 B 原先可能以 `brokerId=1` 注册相同的 `IP:PORT`，切换后又以 `brokerId=0` 注册。如果它在同一组里同时保留两种身份，客户端就可能看到矛盾的地址表。

这行代码删除同一地址的旧 ID 映射；**它并不是整套防双主机制**。注册仍涉及其他状态检查，Controller 的选主与 Broker 的角色切换也不能省略。

如果我们从上面的旧表出发，B 以新 Master 身份注册后，所关心的路由变化是：

```text
旧：broker-a {0: A:10911, 1: B:10911}
新：broker-a {0: B:10911}
```

至此，服务端路由*有机会*变正确了。但生产者会立即知道吗？还不会。

## NameServer 已经更新，为什么客户端还拿着旧地址？

如果生产者每发送一条消息，都先问 NameServer “现在 Master 是谁”，就能减少长期持有旧地址的问题。但这也意味着每条消息都多一次路由查询；如果路由服务暂时不可用，原本已知且可用的地址也无法直接利用。

所以最直觉的优化是：**查询一次后，把路由缓存到客户端；有变化时再更新。**

于是我们得到两个可能并存、却并不相同的状态：

```text
NameServer 内存路由：broker-a → B
Producer 本地缓存：broker-a → A
```

不再是 NameServer 搞错了，而是双方尚未完成同步。

回到真实源码：NameServer 负责从自己的路由表构建 Topic 视图。

**真实源码节选**，文件：[`RouteInfoManager.pickupTopicRouteData()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/namesrv/src/main/java/org/apache/rocketmq/namesrv/routeinfo/RouteInfoManager.java#L3352-L3395)：

```java
Map<String, QueueData> queueDataMap = this.topicQueueTable.get(topic);
if (queueDataMap != null) {
    topicRouteData.setQueueDatas(new ArrayList<>(queueDataMap.values()));
```

这一段只截取与当前问题有关的队列数据部分；后面同一方法还把对应的 BrokerData（包括地址映射）加入 `TopicRouteData`。因此客户端按 Topic 询问时，得到的不是“某台机器是否活着”的布尔值，而是一份可以用于后续选队列、找地址的路由描述。

客户端拿到它后，必须实际更新自己的缓存。

**真实源码节选**，文件：[`client/.../factory/MQClientInstance.java`，`updateTopicRouteInfoFromNameServer()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/factory/MQClientInstance.java#L3786-L3902)：

```java
topicRouteData = this.mQClientAPIImpl.getTopicRouteInfoFromNameServer(topic, clientConfig.getMqClientApiTimeout());
```

随后在路由确实需要更新的分支中，可以看到：

```java
for (BrokerData bd : topicRouteData.getBrokerDatas()) {
    this.brokerAddrTable.put(bd.getBrokerName(), bd.getBrokerAddrs());
}
```

- **调用者**：客户端路由刷新任务，或需要首次获取发布路由的代码。
- **输入**：Topic，以及 NameServer 返回的 `TopicRouteData`。
- **动作**：将 BrokerName 对应的 BrokerId→地址映射更新到客户端地址缓存；同一流程还更新生产者可发布队列信息、消费者订阅队列信息。
- **输出/副作用**：客户端下一次通过 BrokerName 找发送地址时，有机会拿到 B 的地址。
- **没有做到的事情**：不会因为 Controller 刚选主，就在所有客户端上瞬间、原子地更换地址。

那具体怎样触发这次刷新？固定版本的 `MQClientInstance` 会以 `pollNameServerInterval` 为间隔安排周期性的 Topic 路由更新任务（[`startScheduledTask()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/factory/MQClientInstance.java#L2857-L2869)）。而在经典生产者首次缺少有效发布路由时，`DefaultMQProducerImpl.tryToFindTopicPublishInfo()` 也会触发查询。

注意一个容易误读的细节：**发送失败不等于 SDK 在同一次重试里必然刷新 NameServer 路由。** 固定版本的普通发送路径可能先按已缓存的 `TopicPublishInfo` 重试选队列；是否已经拿到新 Broker 地址，取决于路由更新及其时序。我们不能把“有重试”直接翻译成“必定马上知道新主”。

源码位置：[`DefaultMQProducerImpl.sendDefaultImpl()` 与 `sendKernelImpl()`](https://github.com/apache/rocketmq/blob/efa1be59d6fc747ad220f036c86030f05f0419f9/client/src/main/java/org/apache/rocketmq/client/impl/producer/DefaultMQProducerImpl.java#L3410-L3751)。在发送内核入口，先由队列取得 BrokerName，再按 BrokerName 查地址：

```java
String brokerName = this.mQClientFactory.getBrokerNameFromMessageQueue(mq);
String brokerAddr = this.mQClientFactory.findBrokerAddressInPublish(brokerName);
```

这正好把前面的三层联系起来：**Topic 决定候选消息队列，BrokerName 将队列关联到 Broker 组，客户端的地址缓存最终决定发给哪个网络端点。**

## 一个容易遗漏的边界：NameServer 不等于 Controller

到这里，可以把本章才出现的组件放在一张小图中：

```text
               Controller
             决定 B 成为 Master
                     │
               Broker B 切换角色
                     │
                 B 注册身份
                     ▼
               NameServer
             保存 Topic 路由
                     │
                 客户端查询
                     ▼
               Producer 缓存
                     │
               向 B 发送消息
                     ▼
                 Broker B
```

这张图是因果关系，不表示每条箭头都由一个同步 RPC 直接完成。特别是 Controller 的选主决定与 NameServer 的路由更新，是**两个不同的控制过程**。

官方的 [Controller 自动主从切换部署文档](https://rocketmq.apache.org/docs/deploymentOperations/03autofailover/) 也明确区分了 Controller 选主与 NameServer 路由发现；Controller 可独立部署，也可嵌入 NameServer，但嵌入不意味着两者职责合并。

还要注意，多个 NameServer 之间通常不通过强一致协议同步每次路由修改；Broker 可以向多个 NameServer 注册，因此不同实例在短暂窗口里可能给出不同结果。这是本章实验要专门展示的一个边界。

## 最小实验：分别观察三个状态

现在可以写代码了。但这一次，我们不会一开始模拟整个 RocketMQ，而只写三个恰好必要的对象：

1. `Broker`：能否连接、是否允许写入。
2. `NameServer`：记录 Topic→BrokerName→BrokerId→地址。
3. `Producer`：缓存一次查询结果，显式刷新后才更新地址。

**以下是教学化简，不是 RocketMQ 源码**。它只验证路由变化传播的因果关系；不实现网络 RPC、NameServer 过期剔除、Controller 任期验证、静态 Topic、客户端完整重试策略。特别是 `Broker.writable` 只是上章角色切换生效后的简化结果，不代表真实 Broker 每次写入都询问 Controller。

为方便你直接放进 Docusaurus，完整 Python 实现与单元测试全部写在下面。复制代码块的全部内容，保存为 `chapter16.py`，即可执行；无需 API Key、外部依赖或 RocketMQ 服务。

```python title="chapter16.py"
"""教学化简：独立模拟 NameServer 路由与生产者缓存；非 RocketMQ 实现。"""
from dataclasses import dataclass, field
import sys
import unittest


TOPIC = "OrderRecovery"
GROUP = "broker-a"
EVENT = {"event_id": "replay:msg-2002", "order_id": 2002}


class RouteError(Exception):
    pass


@dataclass
class Broker:
    address: str
    alive: bool = True
    writable: bool = False
    stored: list = field(default_factory=list)

    def put(self, event):
        if not self.alive:
            raise ConnectionError(f"{self.address} 无法连接")
        if not self.writable:
            raise PermissionError(f"{self.address} 当前不是可写主节点")
        self.stored.append(dict(event))
        return "ACK"


class NameServer:
    """保留 BrokerName -> {BrokerId -> 地址} 的教学版路由快照。"""

    def __init__(self):
        self.routes = {}  # topic -> broker_name -> broker_id -> address

    def register(self, topic, broker_name, broker_id, address):
        ids = self.routes.setdefault(topic, {}).setdefault(broker_name, {})
        # 角色改变时，同一地址不应同时出现在两个 BrokerId 下。
        for old_id, old_address in list(ids.items()):
            if old_id != broker_id and old_address == address:
                del ids[old_id]
        ids[broker_id] = address

    def query(self, topic):
        # 返回副本，不把注册表暴露给客户端修改。
        return {name: dict(ids) for name, ids in self.routes.get(topic, {}).items()}


class Producer:
    """显式 refresh；模拟客户端缓存的时间差，不冒充 SDK 的自动重试策略。"""

    def __init__(self, namesrv, brokers, topic=TOPIC):
        self.namesrv = namesrv
        self.brokers = brokers  # address -> Broker
        self.topic = topic
        self.cached_route = {}

    def refresh(self):
        self.cached_route = self.namesrv.query(self.topic)
        return self.cached_route

    def send(self, event, broker_name=GROUP):
        ids = self.cached_route.get(broker_name, {})
        address = ids.get(0)  # 教学中 0 表示当前可写 Master
        if address is None:
            raise RouteError(f"缓存中没有 {broker_name} 的 Master 地址")
        broker = self.brokers.get(address)
        if broker is None:
            raise ConnectionError(f"{address} 未在模拟网络中")
        return address, broker.put(event)


def demo():
    a = Broker("10.0.0.1:10911", writable=True)
    b = Broker("10.0.0.2:10911", writable=False)
    network = {a.address: a, b.address: b}
    ns = NameServer()
    ns.register(TOPIC, GROUP, 0, a.address)
    ns.register(TOPIC, GROUP, 1, b.address)
    producer = Producer(ns, network)
    producer.refresh()
    print("① 初始缓存：", producer.cached_route[GROUP])
    print("   初始发送：", producer.send(EVENT)[0])

    # 前章的选主结果已经生效，但注册和客户端刷新暂未发生。
    a.alive, a.writable = False, False
    b.writable = True
    try:
        producer.send({"event_id": "replay:msg-2002-next", "order_id": 2002})
    except ConnectionError as e:
        print("② 新主已产生，旧缓存发送失败：", e)

    # Broker B 向 NameServer 报告自己以 BrokerId=0 担任 Master。
    ns.register(TOPIC, GROUP, 0, b.address)
    print("③ NameServer 新路由：", ns.query(TOPIC)[GROUP])
    print("   客户端仍是旧缓存：", producer.cached_route[GROUP])
    producer.refresh()
    print("④ 刷新后发送到：", producer.send({"event_id": "replay:msg-2002-next", "order_id": 2002})[0])
    print("   B 收到的新增消息数量：", len(b.stored))


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.a = Broker("A", writable=True)
        self.b = Broker("B")
        self.network = {"A": self.a, "B": self.b}
        self.ns = NameServer()
        self.ns.register(TOPIC, GROUP, 0, "A")
        self.ns.register(TOPIC, GROUP, 1, "B")
        self.p = Producer(self.ns, self.network)
        self.p.refresh()

    def promote_b(self):
        self.a.alive = False
        self.a.writable = False
        self.b.writable = True

    def test_initial_route_can_write_to_a(self):
        self.assertEqual(self.p.send(EVENT)[0], "A")

    def test_role_switch_does_not_update_cached_route(self):
        self.promote_b()
        self.assertEqual(self.p.cached_route[GROUP][0], "A")
        with self.assertRaises(ConnectionError):
            self.p.send(EVENT)

    def test_refresh_before_b_registration_still_sees_a(self):
        self.promote_b()
        self.p.refresh()
        self.assertEqual(self.p.cached_route[GROUP][0], "A")

    def test_register_b_does_not_push_update_to_producer(self):
        self.promote_b()
        self.ns.register(TOPIC, GROUP, 0, "B")
        self.assertEqual(self.ns.query(TOPIC)[GROUP][0], "B")
        self.assertEqual(self.p.cached_route[GROUP][0], "A")

    def test_refresh_after_registration_sends_to_b(self):
        self.promote_b()
        self.ns.register(TOPIC, GROUP, 0, "B")
        self.p.refresh()
        self.assertEqual(self.p.send(EVENT)[0], "B")
        self.assertEqual(self.b.stored[0], EVENT)

    def test_new_master_still_unavailable_fails_after_refresh(self):
        self.promote_b()
        self.b.alive = False
        self.ns.register(TOPIC, GROUP, 0, "B")
        self.p.refresh()
        with self.assertRaises(ConnectionError):
            self.p.send(EVENT)

    def test_b_cannot_write_before_promotion(self):
        self.ns.register(TOPIC, GROUP, 0, "B")
        self.p.refresh()
        with self.assertRaises(PermissionError):
            self.p.send(EVENT)

    def test_old_a_reachable_but_demoted_rejects_write(self):
        self.a.writable = False
        with self.assertRaises(PermissionError):
            self.p.send(EVENT)

    def test_b_promotion_removes_old_slave_id(self):
        self.ns.register(TOPIC, GROUP, 0, "B")
        self.assertNotIn(1, self.ns.query(TOPIC)[GROUP])

    def test_nameservers_can_temporarily_disagree(self):
        ns2 = NameServer()
        ns2.register(TOPIC, GROUP, 0, "A")
        self.ns.register(TOPIC, GROUP, 0, "B")
        self.assertNotEqual(self.ns.query(TOPIC), ns2.query(TOPIC))

    def test_no_master_route_fails_explicitly(self):
        p = Producer(NameServer(), self.network)
        p.refresh()
        with self.assertRaises(RouteError):
            p.send(EVENT)

    def test_snapshot_mutation_does_not_change_nameserver(self):
        data = self.ns.query(TOPIC)
        data[GROUP][0] = "not-a-broker"
        self.assertEqual(self.ns.query(TOPIC)[GROUP][0], "A")

    def test_one_topic_does_not_overwrite_another_topic(self):
        self.ns.register("OtherTopic", "broker-x", 0, "B")
        self.ns.register(TOPIC, GROUP, 0, "B")
        self.assertEqual(self.ns.query("OtherTopic")["broker-x"][0], "B")
        self.assertEqual(self.ns.query(TOPIC)[GROUP][0], "B")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        unittest.main(argv=[sys.argv[0]], verbosity=2)
    else:
        demo()
```

运行：

```bash
python chapter16.py
python chapter16.py test
```

### 已验证的运行轨迹

以下结果来自实际执行上面的完整代码（输出只保留关键行）：

```text
① 初始缓存： {0: '10.0.0.1:10911', 1: '10.0.0.2:10911'}
   初始发送： 10.0.0.1:10911
② 新主已产生，旧缓存发送失败： 10.0.0.1:10911 无法连接
③ NameServer 新路由： {0: '10.0.0.2:10911'}
   客户端仍是旧缓存： {0: '10.0.0.1:10911', 1: '10.0.0.2:10911'}
④ 刷新后发送到： 10.0.0.2:10911
   B 收到的新增消息数量： 1
```

实际运行了 **13 项 `unittest` 测试，全部通过**，覆盖：

- 主节点改变，但客户端缓存不会自动变化；
- Broker B 尚未注册时，刷新也不能凭空找到新主；
- B 注册后，NameServer 改了但客户端旧缓存仍未改；
- 刷新后能向 B 发送；
- 新 Master 自己不可用时，刷新也不能保证成功；
- 不同 NameServer 在更新窗口内可能返回不同路由；
- 旧主可连接但已降级时，不能仅凭“地址可达”就当作可写。

这里真正验证的是**我们编写的教学模型**。没有运行真实 RocketMQ 集群，也没有据此声称任何路由传播时延、吞吐或故障恢复 SLA。

## 回到真实系统：为什么不能只靠一次发送重试？

此时我们可以完整解释 `replay:msg-2002-next` 的失败过程：

```text
T0: Producer 通过 NameServer 查询到 A（Master）并缓存
T1: A 失联，Controller 选出 B
T2: B 切换角色，向 NameServer 重新注册
T3: NameServer 查询结果已经指向 B
T4: Producer 仍以旧缓存向 A 发消息，连接失败
T5: Producer 路由刷新，缓存更新为 B
T6: 下一次发送使用 B 的地址，消息成功写入
```

这个序列中特别容易混淆的是 T2 与 T5：**路由已经在 NameServer 正确，并不等于生产者已经更新。** 如果 T4 的请求只得到超时，而没有明确知道消息是否被保存，就还需要沿用前面章节学习过的重试与幂等保护，不能草率得出“这条消息一定没发送成功”。

也不要把 `brokerId=0` 的缓存示例理解成 RocketMQ 在所有消费模式中都永远只能向这个 ID 通信。具体客户端协议、读写路径、静态队列及 5.x Proxy 模式可能有额外转换。本章固定的是经典 Java 生产者沿 BrokerName 解析 Broker 地址的主路径。

## 回看演化：我们只为一条消息增加了什么？

从第 14 章开始，问题实际上被拆成了三个独立层次：

```text
第 14 章 复制：B 是否保存了所需消息？
第 15 章 选主：B 是否被允许成为新 Master？
第 16 章 路由：生产者现在是否知道要向 B 发送？
```

三件事缺一不可。数据副本解决不了地址发现；地址发现也不能替代选主；Controller 的选主结果本身又不能保证所有客户端瞬时更新。

到这里，订单 `2002` 的新消息终于能够在主节点故障之后找到 B。我们仍然有一个尚未解决的问题：**原来正在 A 上拉取消息的消费者怎么办？它手里保存的消费进度、队列分配与 Broker 地址是否也会随切换正确恢复？**

下一章，我们就从这个已经运行一半的消费者开始，追踪故障恢复后的消费进度与重新分配。

---

## 本章源码索引与验证边界

**固定仓库：** [apache/rocketmq](https://github.com/apache/rocketmq)；**版本：** 5.5.1；**Commit：** `efa1be59d6fc747ad220f036c86030f05f0419f9`。

| 核对入口 | 这次要解释的动作 |
| --- | --- |
| `BrokerOuterAPI.registerBrokerAll()` | Broker 构造并发送路由注册请求 |
| `RouteInfoManager.registerBroker()` | NameServer 维护 BrokerId→地址映射 |
| `RouteInfoManager.pickupTopicRouteData()` | NameServer 按 Topic 组装路由数据 |
| `MQClientInstance.updateTopicRouteInfoFromNameServer()` | 客户端查询并更新 Broker 与队列信息缓存 |
| `MQClientInstance.startScheduledTask()` | 定时更新 Topic 路由 |
| `DefaultMQProducerImpl.sendKernelImpl()` | 生产者根据 BrokerName 查找发送地址 |

所有标为“真实源码节选”的代码均对应上述固定 Commit；原代码可能还有校验、兼容、权限、锁及异常处理，本章未展示的部分不能据此推断不存在。所有完整 Python 代码都是教学化简，已单独运行测试，但不声称与 RocketMQ 的具体网络时序逐行一致。
