---
title: "第一章：一条 SET 命令在 Redis 内部经历了什么？"
sidebar_label: "01 - 从 SET 命令开始"
sidebar_position: 2
description: 从最简单的 SET 命令出发，沿 Redis 7.2.0 真实源码追踪网络读取、协议解析、命令分发、数据写入和结果返回的完整过程。
tags:
  - Redis
  - 源码精读
  - SET
  - 命令执行
---

# 第一章：一条 SET 命令在 Redis 内部经历了什么？

在引言中，我们从一个简单的 Python 字典出发：

```python
store = {}

store["name"] = "redis"

print(store["name"])
```

这段代码很容易理解。

当执行 `store["name"] = "redis"` 时，Python 会在字典中保存一个键值关系。

但如果希望另一个进程也能访问这些数据，情况就不一样了。

我们不能再直接操作本地字典，而是需要通过网络向一个独立的数据服务发送请求。

Redis 正是这样一个能够通过网络访问的数据服务。

现在，我们先不关心 Redis 为什么快，也不关心它支持多少种数据结构。

只研究一条命令：

```redis
SET name redis
```

它看起来与 Python 字典赋值没有太大区别。

但我们知道，客户端和 Redis 服务端运行在不同的进程中。

那么，当我们敲下这条命令时，Redis 究竟是怎样收到请求、理解命令、修改数据，再把结果返回给客户端的？

这就是本章要解决的唯一问题。

## 1. 从一个最小实验开始

首先，我们需要一个可以运行的 Redis 实例。

整个系列统一使用 Redis 7.2.0 作为源码分析基线。

如果已经安装了 Redis 7.2.0，可以直接使用本地环境。

也可以通过 Docker 启动一个独立实例：

```bash
docker run -d \
  --name redis-source-lab \
  -p 127.0.0.1:6379:6379 \
  redis:7.2.0 \
  --save "" \
  --appendonly no
```

这里特意关闭了 RDB 和 AOF 持久化，只是为了让实验集中在内存命令的执行过程上。不要将这个配置直接用于需要数据持久化的生产环境。

现在执行：

```bash
docker exec redis-source-lab redis-cli SET name redis
```

预期输出：

```text
OK
```

再执行：

```bash
docker exec redis-source-lab redis-cli GET name
```

预期输出：

```text
redis
```

这说明 Redis 已经保存了我们写入的值。

但是，刚才究竟发生了什么？

从客户端来看，只是执行了一条命令；从服务端来看，至少需要完成四件事：

```text
客户端
   |
   | 发送 SET name redis
   v
Redis
   |
   | ① 接收网络数据
   | ② 理解 SET 命令
   | ③ 写入内存
   | ④ 生成并返回结果
   v
客户端收到 OK
```

我们先尝试实现第一步。

## 2. 客户端怎样告诉 Redis 要执行什么？

如果自己编写一个远程字典服务，最直接的方法可能是发送一段文本：

```text
SET name redis
```

服务端可以按照空格拆分：

```python
# 概念伪代码

parts = request.split(" ")

command = parts[0]
key = parts[1]
value = parts[2]
```

对于当前输入，得到：

```text
command = SET
key     = name
value   = redis
```

这种方法在简单情况下完全可以工作。

但如果我们希望保存的 Value 本身就包含空格呢？

例如：

```text
SET message hello redis
```

这里的 `hello redis` 究竟是一个 Value，还是两个参数？

如果 Value 中包含换行符、二进制数据，又应该怎样确定一条请求的结束位置？

仅靠空格拆分，就不再足够可靠。

我们需要一种能够明确描述参数数量和每个参数长度的传输格式。

Redis 使用的通信协议叫 RESP（Redis Serialization Protocol）。

对于我们当前的命令：

```redis
SET name redis
```

客户端通常会将它编码为下面的 RESP 格式：

```text
*3\r\n
$3\r\n
SET\r\n
$4\r\n
name\r\n
$5\r\n
redis\r\n
```

这里的 `\r\n` 表示协议中的回车换行字节，而不是客户端需要手动输入的文字。

这段内容看起来比原来的命令复杂了一些，但规则非常简单。

`*3` 表示这是一个包含三个元素的数组。

`$3` 表示接下来的字符串有三个字节，所以读取 `SET`。

`$4` 表示接下来的字符串有四个字节，所以读取 `name`。

`$5` 表示接下来的字符串有五个字节，所以读取 `redis`。

这样，即使参数中包含空格，Redis 也不需要依赖空格判断参数边界。

**问题从“如何拆分一段字符串”，变成了“如何按照协议准确读取每一个参数”。**

这就是 Redis 在客户端和服务端之间使用通信协议的原因。

现在，客户端能够表达一条完整命令了。

但 Redis 服务端又是怎样接收到这些字节的？

## 3. Redis 从哪里接收客户端请求？

现在开始进入真实源码。

本章源码统一对应 Redis 7.2.0。

首先定位到：

- 文件：`src/networking.c`
- 函数：`createClient()`
- 函数：`readQueryFromClient()`

完整源码：[networking.c](https://github.com/redis/redis/blob/7.2.0/src/networking.c)

当一个客户端建立连接后，Redis 会为它创建相应的客户端状态。

在 `createClient()` 中，有一行非常关键的代码。

**真实源码节选：**

```c
connSetReadHandler(conn, readQueryFromClient);
```

这里暂时不需要理解整个 `client` 结构，也不需要研究底层事件循环。

只需要看清楚这行代码的作用：

Redis 为这个连接设置了一个读取处理函数。

当连接上有数据需要读取时，就可以调用 `readQueryFromClient()`。

这解决了我们刚才面临的第一个问题：

**服务端如何知道应该由哪个函数接收客户端发送的数据？**

现在继续进入 `readQueryFromClient()`。

这个函数内部会从网络连接读取数据。

其中一行关键代码是：

**真实源码节选：**

```c
nread = connRead(c->conn, c->querybuf+qblen, readlen);
```

我们只需要关心三个东西：

- `c->conn`：当前客户端的连接。
- `c->querybuf`：保存客户端请求数据的缓冲区。
- `nread`：本次实际读取到的字节数量。

所谓缓冲区，可以先理解为一块临时保存数据的内存区域。

为什么需要它？

因为网络传输不保证一次读取操作就能拿到一条完整命令。

例如，客户端发送：

```redis
SET name redis
```

Redis 可能先收到前面一部分字节，之后才收到剩余内容。

如果每收到一点数据就立即尝试执行，显然会出问题。

因此，Redis 需要先把收到的字节放进请求缓冲区，再尝试从中解析完整命令。

到这里，我们已经看到一个简单的变化：

```text
原来的 Python 字典：

store["name"] = "redis"


现在的 Redis：

客户端
   |
   | 网络发送请求
   v
readQueryFromClient()
   |
   v
c->querybuf
```

但缓冲区里现在保存的仍然是原始请求数据。

Redis 还不知道这条命令应该调用哪个处理函数。

下一步就是解析。

## 4. Redis 怎样把字节变成命令参数？

继续阅读 `src/networking.c`。

`readQueryFromClient()` 在读取请求数据之后，会进入：

```c
processInputBuffer(c)
```

这里的 `c` 就是当前客户端对应的状态对象。

在 `processInputBuffer()` 中，Redis 首先需要判断请求采用哪一种协议格式。

对于常见 Redis 客户端发送的 RESP 数组请求，Redis 会使用多元素请求的解析逻辑。

对应的函数是：

```c
processMultibulkBuffer(c)
```

它负责按照 RESP 的格式读取参数。

对当前的 `SET name redis` 来说，解析完成后，我们可以把客户端状态教学化地表示为：

```c
// 教学化简：只展示当前请求涉及的字段

c->argc = 3;

c->argv[0] = "SET";
c->argv[1] = "name";
c->argv[2] = "redis";
```

这里有两个此前没有见过的字段：

`argc` 表示参数数量。

`argv` 表示解析完成后的参数数组。

它们的名字沿用了 C 程序中常见的参数表示方式。

注意，这只是为了方便理解而写出的等价表示。实际的 `argv` 元素是 Redis 对象指针，并不是普通的 C 字符串。

现在发生了一件非常重要的事情：

原来 Redis 收到的是一段网络字节流，而现在 Redis 已经得到了结构化的命令参数。

```text
客户端发送的字节
        |
        v
readQueryFromClient()
        |
        v
请求缓冲区 querybuf
        |
        v
processInputBuffer()
        |
        v
processMultibulkBuffer()
        |
        v
argc = 3

argv[0] = SET
argv[1] = name
argv[2] = redis
```

那么，解析完成之后，Redis 怎样继续执行？

在普通的命令执行路径中，`processInputBuffer()` 会调用：

```c
processCommandAndResetClient(c)
```

随后，该函数进一步调用：

```c
processCommand(c)
```

这里我们终于从网络处理进入了命令处理阶段。

不过仍然存在一个问题。

Redis 支持很多命令，不同命令对应的操作完全不同。

那么，Redis 怎样知道 `SET` 应该执行哪一段代码？

## 5. Redis 如何找到 SET 对应的函数？

我们先不看源码，尝试自己设计一下。

假设只有两条命令：

```text
SET name redis
GET name
```

最简单的实现可能是：

```python
# 概念伪代码

if command == "SET":
    store[key] = value

elif command == "GET":
    return store.get(key)
```

这个方案在只有两条命令时没有问题。

但如果命令越来越多，我们就需要不断添加新的判断分支。

更自然的思路，是让命令名称与处理函数建立映射关系。

例如：

```python
# 概念伪代码

commands = {
    "SET": set_command,
    "GET": get_command,
}

handler = commands[command]
handler(arguments)
```

这样，命令解析和命令实现就分开了。

Redis 也采用了类似的设计。

Redis 7.2.0 的命令定义信息包含在自动生成的 `src/commands.def` 中。

其中，`SET` 的命令定义关联到：

```c
setCommand
```

这意味着，当 Redis 识别出 `SET` 后，它能够找到对应的处理函数。

现在进入 `src/server.c`，查看 `processCommand()`。

**真实源码节选：**

```c
c->cmd = c->lastcmd = c->realcmd = lookupCommand(c->argv,c->argc);
```

我们先只关心右侧：

```c
lookupCommand(c->argv,c->argc)
```

它根据当前请求的参数查找 Redis 命令定义。

对于：

```text
SET name redis
```

它会找到 `SET` 的命令信息，而这个命令的处理函数是 `setCommand()`。

当然，Redis 并不会找到函数后就立即无条件执行。

`processCommand()` 还需要检查命令是否存在、参数数量是否正确，以及当前客户端是否有权限执行等条件。

这些检查非常重要，但我们先沿着一个合法请求的正常路径继续阅读。

通过检查后，普通命令会进入 `call()`。

在 `call()` 中，真正调用命令处理函数的是：

**真实源码节选 — `src/server.c`：**

```c
c->cmd->proc(c);
```

这一行就是命令分发过程的关键。

`c->cmd` 保存当前命令的信息。

`proc` 指向对应的处理函数。

对我们的 `SET` 来说，实际调用的就是：

```c
setCommand(c);
```

现在回头看，Redis 为什么没有直接在解析网络请求的地方完成字典写入？

原因就很清楚了。

**网络解析只负责还原客户端发来的命令，命令分发负责找到应该执行的操作，具体的数据修改则由对应的命令函数完成。**

这让 Redis 可以增加新的命令，而不必把所有命令逻辑全部写在网络读取函数中。

接下来，我们终于可以研究 `SET` 本身如何修改数据了。

## 6. SET 最终在哪里写入数据？

现在定位到：

- 文件：`src/t_string.c`
- 函数：`setCommand()`
- 函数：`setGenericCommand()`

完整源码：[t_string.c](https://github.com/redis/redis/blob/7.2.0/src/t_string.c)

我们已经知道 `processCommand()` 最终会执行 `setCommand()`。

但进入这个函数后会发现，它并没有直接操作底层字典。

它首先检查 `SET` 的扩展参数，并对待写入的值进行编码处理，然后调用：

**真实源码节选：**

```c
setGenericCommand(c,flags,c->argv[1],c->argv[2],expire,unit,NULL,NULL);
```

为什么还需要一个 `setGenericCommand()`？

因为 Redis 不仅支持最简单的 `SET key value`，还允许设置过期时间、条件写入等附加行为。

这些操作有很多共同的写入逻辑。

因此，Redis 将公共部分集中到 `setGenericCommand()` 中，而不是为每一种写入形式都重复实现一套完整过程。

不过，当前我们没有使用任何附加参数。

所以只需要跟踪：

```redis
SET name redis
```

在 `setGenericCommand()` 中，Redis 首先需要判断当前 Key 是否已经存在。

**真实源码节选：**

```c
found = (lookupKeyWrite(c->db,key) != NULL);
```

这里的 `c->db` 是当前客户端正在操作的 Redis 逻辑数据库。

`lookupKeyWrite()` 用于查找准备写入的 Key，并按写操作的语义处理相关状态。

对于第一次执行：

```redis
SET name redis
```

如果 `name` 尚不存在，那么 `found` 就为假。

对于第二次执行：

```redis
SET name hello
```

如果 `name` 仍然存在，那么 `found` 就为真。

为什么写入之前需要知道 Key 是否存在？

回想 Python 字典：

```python
store["name"] = "redis"
```

如果 `name` 不存在，就创建新的键值关系。

如果 `name` 已经存在，就替换原来的值。

虽然代码看起来完全一样，但底层需要做的事情并不相同。

Redis 也需要区分这两种情况。

在完成相关判断后，`setGenericCommand()` 会调用：

**真实源码节选：**

```c
setKey(c,c->db,key,val,setkey_flags);
```

这里就是本章最关键的写入入口。

但是，这一行仍然没有告诉我们数据到底保存在哪里。

继续进入 `setKey()`。

## 7. Redis 的 Key-Value 最终存放在哪里？

现在来到：

- 文件：`src/db.c`
- 函数：`setKey()`
- 函数：`dbAdd()`
- 函数：`dbSetValue()`

完整源码：[db.c](https://github.com/redis/redis/blob/7.2.0/src/db.c)

在 Redis 内部，`redisDb` 保存一个逻辑数据库的相关状态。

其中有一个非常重要的字段：

```c
db->dict
```

它指向当前数据库用于保存键值关系的字典。

这与我们最开始使用 Python 字典的设计非常相似。

当然，Redis 使用的是自己实现的 C 语言字典结构，而不是 Python 的 `dict`。

为了理解当前写入过程，可以先把它看成：

```text
redisDb
   |
   v
 dict
   |
   +-- "name"  --->  "redis"
```

真实存储中还包含 Redis 对象、内存管理和其他信息，但现在不需要展开。

我们只需要确认：

**Redis 最终需要在 `db->dict` 中建立或者更新 Key 与 Value 的关联。**

在 `setKey()` 中，Redis 根据 Key 是否已经存在选择不同操作。

如果 Key 不存在，调用：

**真实源码节选：**

```c
dbAdd(db,key,val);
```

继续进入 `dbAdd()`，最终会进入 `dbAddInternal()`。

在这个函数中，Redis 通过字典操作创建新的条目，再保存 Key 和对应的 Value。

其中两个关键操作是：

```c
dictSetKey(db->dict, de, sdsdup(key->ptr));
dictSetVal(db->dict, de, val);
```

第一行设置字典条目的 Key。

第二行设置字典条目的 Value。

到这里，新的键值关系才真正进入 Redis 数据库的内存结构。

如果当前数据库原本是：

```text
db->dict

(empty)
```

执行：

```redis
SET name redis
```

之后就变成：

```text
db->dict

"name"  --->  Redis String 对象("redis")
```

注意，以上示意图表示逻辑上的映射关系，并不意味着 Redis 直接把两个普通 C 字符串原样放进一个简单数组。

Redis 会使用自己的字符串表示和对象管理机制。

这些细节我们后续在研究数据存储时再展开。

现在看第二种情况。

如果执行：

```redis
SET name hello
```

此时 `name` 已经存在，普通 `SET` 就需要替换原来的值。

在 `setKey()` 对应的已存在分支中，调用的是：

```c
dbSetValue(db,key,val,1,NULL);
```

`dbSetValue()` 会找到字典中已有的条目，将其中的 Value 替换为新值，并处理旧值的释放等相关工作。

因此，数据变化是：

```text
执行前：

"name"  --->  "redis"


执行：

SET name hello


执行后：

"name"  --->  "hello"
```

现在，我们终于能够解释最开始的 `SET` 操作了。

它并不是一个神秘的远程赋值函数。

对于这条最简单的命令，Redis 最终还是需要完成和普通字典类似的操作：

**找到 Key；如果不存在就新增，如果存在就更新对应的 Value。**

不过，Redis 把这个过程拆分成了不同的职责：

- `setCommand()` 处理 SET 命令的入口和参数。
- `setGenericCommand()` 组织通用写入逻辑。
- `setKey()` 负责数据库层面的设置操作。
- `dbAdd()` / `dbSetValue()` 负责新增或更新实际字典条目。

这种分层不是为了让一行赋值变复杂。

而是因为 Redis 面向的是一个长期运行、需要支持不同写入命令和数据管理规则的服务。

简单的 Python 字典只需要考虑当前程序里的赋值操作，而 Redis 的数据库操作还需要协调对象引用、过期状态、修改通知等行为。

这些附加机制先不展开。

现在，数据已经写入内存。

但客户端为什么会收到 `OK`？

## 8. Redis 怎样把结果返回给客户端？

回到 `src/t_string.c` 的 `setGenericCommand()`。

在完成正常写入后，如果没有使用要求返回旧值的 `GET` 选项，Redis 会添加成功回复。

**真实源码节选：**

```c
addReply(c, ok_reply ? ok_reply : shared.ok);
```

对于普通的 `SET name redis`，`ok_reply` 没有指定，因此使用 `shared.ok`。

但这里要注意：

`addReply()` 并不是简单地在调用位置直接向网络发送所有响应字节。

它会将响应加入客户端的输出缓冲区。

后续再由 Redis 的网络写入逻辑将数据发回客户端。

对于 RESP 协议，普通 `SET` 成功时的响应为：

```text
+OK\r\n
```

其中 `+` 表示一个简单字符串响应。

客户端收到之后，就能够将它显示为：

```text
OK
```

这就解释了我们最开始观察到的结果。

不过还有一个边界需要明确：

**客户端收到 `OK`，表示这条 SET 命令在当前 Redis 实例上执行成功；并不自动意味着数据已经安全写入磁盘，或者已经复制到其他节点。**

持久化和复制是另外的问题，我们会等到对应的故障场景出现时再研究。

现在只需要确认，这条请求的正常执行路径已经完整闭合。

## 9. 回头看：一条 SET 命令的完整执行链

到这里，我们已经认识了整条正常执行路径中的关键函数。

现在才适合把它们放到同一张图中：

```text
客户端
  |
  | SET name redis
  | RESP 编码后的字节
  v
Redis 网络连接
  |
  v
readQueryFromClient()
  |
  | 读取数据
  v
querybuf
  |
  v
processInputBuffer()
  |
  | 解析 RESP
  v
processMultibulkBuffer()
  |
  | argc / argv
  v
processCommandAndResetClient()
  |
  v
processCommand()
  |
  | 查找命令
  | 检查命令和执行条件
  v
call()
  |
  | c->cmd->proc(c)
  v
setCommand()
  |
  v
setGenericCommand()
  |
  | 查找 Key
  v
setKey()
  |
  +---- Key 不存在 ----> dbAdd()
  |
  +---- Key 已存在 ----> dbSetValue()
  |
  v
db->dict
  |
  | 写入完成
  v
addReply()
  |
  | 输出缓冲区
  v
网络写入逻辑
  |
  v
客户端收到 OK
```

注意，这张图表示当前普通请求的主要调用和数据处理路径，并不是 Redis 所有命令在所有执行环境下都会经历的唯一调用栈。

例如，启用 I/O 线程读取、执行事务或运行脚本时，还存在其他处理分支。

但对于理解一条普通 `SET` 命令，这条路径已经足够。

而且，现在图上的每一层都有了明确的作用。

我们不是先背下来一堆函数名，再试图记住它们的调用关系。

而是从一个必须解决的问题开始：

- 客户端不能直接访问 Redis 内存，所以需要网络通信。
- 网络上传输的是字节，所以需要请求协议和解析。
- Redis 支持许多命令，所以需要命令查找与分发。
- SET 需要实际修改数据，所以需要数据库写入操作。
- 客户端需要知道执行结果，所以需要响应机制。

这些机制是为了解决不同问题才逐步出现的。

## 10. 实验：绕过 redis-cli，直接发送 RESP

前面的实验使用了 `redis-cli`。

但 `redis-cli` 已经帮助我们完成了命令编码和网络通信，因此我们看不到真正发送的请求内容。

现在，我们尝试自己发送刚才研究过的 RESP 字节。

不需要额外安装 Python 依赖，只使用标准库中的 `socket`。

创建 `send_set.py`：

```python
import socket

request = (
    b"*3\r\n"
    b"$3\r\nSET\r\n"
    b"$4\r\nname\r\n"
    b"$5\r\nredis\r\n"
)

with socket.create_connection(
    ("127.0.0.1", 6379),
    timeout=3,
) as sock:

    sock.sendall(request)

    with sock.makefile("rb") as reader:
        response = reader.readline()

    print(repr(response))
```

运行：

```bash
python send_set.py
```

预期输出：

```text
b'+OK\r\n'
```

这个实验比使用 `redis-cli` 更接近刚才的源码执行路径。

因为我们亲自构造了请求字节，并通过 TCP 连接发送给 Redis。

Redis 收到这些字节后，仍然会走刚才研究的正常命令处理路径。

现在，我们可以进一步检查结果。

执行：

```bash
docker exec redis-source-lab redis-cli GET name
```

预期输出：

```text
redis
```

这就形成了一个完整的验证：

```text
Python socket
      |
      | 发送 RESP 数据
      v
Redis 网络读取
      |
      v
命令解析与分发
      |
      v
写入 db->dict
      |
      v
返回 +OK
      |
      v
使用 GET 读取保存的值
```

这里的 `GET` 只是用来检查 `SET` 的写入结果。

它在 Redis 7.2.0 中对应 `getCommand()` 和 `getGenericCommand()`，通过数据库查找读取值，再使用 `addReplyBulk()` 将结果加入回复。

本章不单独深入 GET 的全部执行细节，因为它没有改变我们当前需要理解的主问题。

### 再做一次更新实验

先执行：

```bash
docker exec redis-source-lab redis-cli SET name first
```

随后：

```bash
docker exec redis-source-lab redis-cli SET name second
```

最后：

```bash
docker exec redis-source-lab redis-cli GET name
```

预期结果为：

```text
second
```

这个实验可以帮助我们对应源码中的两条分支：

第一次写入不存在的 Key 时，使用新增路径。

再次写入已存在的 Key 时，使用更新路径。

不过，如果需要严格观察第一次新增路径，应先执行：

```bash
docker exec redis-source-lab redis-cli DEL name
```

再进行第一次 SET。

### 实验验证边界

以上命令、源码路径及输出依据 Redis 7.2.0 的实现整理。本文没有在当前编写环境中实际启动 Redis 实例，因此展示的是可复现的实验步骤及预期结果，而不是已经完成的运行记录。

如果完成实验后不再需要容器，可以执行：

```bash
docker rm -f redis-source-lab
```

## 11. 解决了一个问题，又出现了下一个问题

现在，我们已经理解了一条 Redis 命令如何从网络请求变成实际的内存操作。

但仔细回看整个过程，会发现还有一个尚未解释的现象。

刚才，我们只发送了一条 `SET`。

如果有一千个客户端同时向 Redis 发送请求呢？

假设：

```text
客户端 A：SET name alice
客户端 B：GET name
客户端 C：SET count 100
客户端 D：GET count
客户端 E：SET status active
          ...
```

这些请求可能在很短的时间内到达 Redis。

如果服务端为每个客户端创建一个线程，当然也可以实现并发请求处理。

但这样需要管理线程、协调共享数据访问，并处理多个执行线程同时修改数据库的情况。

如果完全顺序处理连接，又会遇到另一种问题：某个客户端长时间没有发送数据时，Redis 是否必须一直等待它，从而无法服务其他客户端？

我们真正需要回答的是：

**一个主要依靠事件驱动执行命令的数据服务，怎样同时管理大量客户端连接，又让命令执行过程保持可控？**

前面我们已经看到：

```c
connSetReadHandler(conn, readQueryFromClient);
```

这行代码只是告诉 Redis，当连接有数据可读时，应该调用哪个处理函数。

但是谁负责发现连接可读？

多个连接同时就绪时，又由谁决定先处理哪个？

这就需要进一步研究 Redis 的事件循环与 I/O 多路复用机制。

不过，我们不需要在本章提前展开它们的全部实现。

现在只需要留下一个清晰的问题：

**Redis 如何在管理大量客户端连接的同时，持续执行这些客户端发送的命令？**

这就是下一章的起点。

---

## 本章回顾

我们从一个最简单的 Python 字典开始：

```python
store["name"] = "redis"
```

然后把它变成一个远程操作：

```redis
SET name redis
```

这一变化迫使系统增加了几个机制。

首先，客户端和数据服务运行在不同进程中，因此需要网络连接。

其次，网络传输的是字节流，因此需要明确的请求协议和参数解析。

然后，服务端支持多种命令，因此需要根据命令名称查找对应的处理函数。

最后，具体的命令处理函数负责访问和修改底层数据结构，再将执行结果交还客户端。

沿着这条路径，我们在 Redis 7.2.0 的真实源码中找到了对应实现：

| 职责 | 关键源码 |
|---|---|
| 读取客户端数据 | `readQueryFromClient()` |
| 处理输入缓冲区 | `processInputBuffer()` |
| 解析 RESP 数组 | `processMultibulkBuffer()` |
| 查找与检查命令 | `processCommand()` |
| 执行命令 | `call()` |
| 处理 SET | `setCommand()` |
| 执行通用写入 | `setGenericCommand()` |
| 设置数据库中的 Key | `setKey()` |
| 新增键值关系 | `dbAdd()` |
| 替换现有 Value | `dbSetValue()` |
| 构造成功响应 | `addReply()` |

至此，我们已经能够解释：

**Redis 如何将一条客户端命令转换为内存中的一次数据修改。**

但我们仍然只理解了一个客户端、一条命令的正常路径。

下一章，我们将让多个客户端同时连接 Redis，从一个具体的等待问题出发，研究 Redis 如何利用事件循环处理网络请求。

**下一章：《Redis 如何同时处理大量客户端？从阻塞等待到事件循环》**

---

## 源码参考

本系列固定源码版本：Redis 7.2.0。

- [networking.c：客户端连接、请求读取、响应写入](https://github.com/redis/redis/blob/7.2.0/src/networking.c)
- [server.c：命令查找、检查与执行](https://github.com/redis/redis/blob/7.2.0/src/server.c)
- [t_string.c：SET 与 GET 命令实现](https://github.com/redis/redis/blob/7.2.0/src/t_string.c)
- [db.c：数据库查询与写入操作](https://github.com/redis/redis/blob/7.2.0/src/db.c)
- [commands.def：Redis 命令定义](https://github.com/redis/redis/blob/7.2.0/src/commands.def)
- [Redis RESP 协议](https://redis.io/docs/latest/develop/reference/protocol-spec/)
- [Redis SET 命令文档](https://redis.io/docs/latest/commands/set/)