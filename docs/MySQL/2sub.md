---
title: "01｜从一次库存扣减开始：我们到底需要数据库做什么？"
sidebar_position: 2
slug: /mysql/01-first-update
description: 从最简单的 Python 库存扣减开始，逐步建立真实 MySQL UPDATE 的输入、执行结果与源码入口。
tags: [MySQL, InnoDB, 源码精读]
---

# 从一次库存扣减开始：我们到底需要数据库做什么？

假设我们正在开发一个商品购买系统。

现在，系统只有一个商品：

```text
商品 ID：42
商品名称：Keyboard
当前库存：100
```

用户购买一件商品，我们需要把库存从 `100` 修改为 `99`。

这是一个很简单的需求。

如果让我们自己实现，可能只需要几行 Python 代码。

但同样的操作交给 MySQL，却会经过一套复杂得多的执行过程。

为什么？

我们先不急着研究 MySQL。

从一个最简单、确实能够工作的版本开始。

## 1. 最简单的库存扣减，只需要一个字典

假设所有商品都保存在 Python 字典中：

```python
products = {
    42: {
        "name": "Keyboard",
        "stock": 100
    }
}
```

字典的键是商品 ID，值是商品信息。

如果用户想购买商品 `42`，我们只需要：

```python
products[42]["stock"] -= 1
```

此时库存已经从 `100` 变成 `99`。

不过，直接减一还有一个明显的问题。

如果库存已经是零，我们就不应该继续扣减。

因此，稍微增加一点判断：

```python
products = {
    42: {
        "name": "Keyboard",
        "stock": 100
    }
}


def buy(product_id):
    product = products.get(product_id)

    if product is None:
        return False

    if product["stock"] <= 0:
        return False

    product["stock"] -= 1
    return True


success = buy(42)

print("购买成功:", success)
print("剩余库存:", products[42]["stock"])
```

实际运行结果：

```text
购买成功: True
剩余库存: 99
```

现在，我们已经有了一个最小可工作的购买程序。

它具备三个能力：

- 可以根据商品 ID 找到对应的商品。
- 只有库存大于零时才允许购买。
- 购买成功之后，库存减少一件。

到这里，没有任何理由必须引入 MySQL。

对于单个 Python 进程中的简单实验，这个方案完全够用。

甚至它还有一个优点：实现足够简单，我们可以直接看清每一步究竟做了什么。

那么，问题出在哪里？

## 2. 程序重新启动之后，库存去哪了？

假设用户已经购买一件商品，库存从 `100` 变成了 `99`。

随后，我们关闭程序，再重新启动它。

程序又执行了最初的初始化代码：

```python
products = {
    42: {
        "name": "Keyboard",
        "stock": 100
    }
}
```

这时库存是多少？

又变回了 `100`。

我们刚才修改的，只是运行过程中保存在内存里的 Python 对象。

程序重新创建这些对象时，并不知道上一次运行曾经卖出过一件商品。

可以把整个过程理解为：

```text
第一次启动程序
        |
        v
初始化库存 = 100
        |
        v
用户购买一件
        |
        v
内存库存 = 99
        |
        v
程序结束

------------------------

第二次启动程序
        |
        v
重新执行初始化代码
        |
        v
内存库存 = 100
```

这不是 Python 的错误。

是我们当前的程序根本没有实现保存业务状态的能力。

当然，我们可以把商品数据写入一个文件，下次启动再从文件读取。

这同样是一种合理的解决办法。

但随后，我们还需要自己决定：

修改什么时候写入文件？

多个程序怎样访问同一份数据？

写入中途发生异常怎么办？

文件越来越大时怎样查找商品？

这些问题现在还不需要全部解决。

当前最明确的需求只有一个：

**商品库存不能只依赖购买程序当前运行时的内存状态。**

我们希望购买程序退出、重新启动甚至换一个程序连接之后，依然能够访问之前保存的商品信息。

因此，我们把商品数据交给一个独立运行、负责管理持久化数据的系统。

这一次，我们使用 MySQL。

## 3. 把商品从 Python 字典搬到 MySQL

先准备一个独立的实验数据库。

后续章节都会继续使用这个商品表。

以下 SQL 可以在 MySQL 8.4 的客户端中执行。

```sql
CREATE DATABASE IF NOT EXISTS mysql_source_lab;

USE mysql_source_lab;

DROP TABLE IF EXISTS products;

CREATE TABLE products (
    id BIGINT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    stock INT NOT NULL
) ENGINE = InnoDB;

INSERT INTO products (id, name, stock)
VALUES (42, 'Keyboard', 100);
```

注意，`DROP TABLE` 会删除实验库中原有的 `products` 表，仅应在专用实验数据库中执行。

现在查询数据：

```sql
SELECT id, name, stock
FROM products
WHERE id = 42;
```

预期结果：

```text
+----+----------+-------+
| id | name     | stock |
+----+----------+-------+
| 42 | Keyboard |   100 |
+----+----------+-------+
```

到这里，我们做的事情与之前初始化 Python 字典很相似。

之前的数据是：

```python
products = {
    42: {"name": "Keyboard", "stock": 100}
}
```

现在的数据则保存在 MySQL 表中。

但有一个关键区别。

Python 字典由我们的 Python 程序创建和管理。

而 MySQL 表中的数据由独立的 MySQL Server 管理。

我们的购买程序不再需要自己持有全部商品数据。

它只需要把操作请求交给 MySQL。

那么，我们怎样告诉 MySQL：

购买商品 `42`，库存减少一件？

## 4. 用一条 UPDATE 代替 Python 函数

先回忆 Python 版本：

```python
def buy(product_id):
    product = products.get(product_id)

    if product is None:
        return False

    if product["stock"] <= 0:
        return False

    product["stock"] -= 1
    return True
```

这段程序的含义很清楚。

先找到商品，判断库存是否充足，再将库存减一。

换成 SQL，可以写成：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

我们暂时不讨论 MySQL 如何执行它。

先从业务角度理解这条 SQL。

`UPDATE products` 表示修改 `products` 表中的记录。

`SET stock = stock - 1` 表示将库存设置为当前库存减一。

`WHERE id = 42 AND stock > 0` 表示只有商品 ID 为 `42`，并且当前库存大于零的记录才符合本次更新条件。

和 Python 代码相比，它们在当前单次购买场景中的业务目标是相同的。

但需要注意，**业务目标相同，不代表两段代码的运行机制相同**。

Python 代码直接修改一个字典对象。

而 SQL 只是在描述我们希望数据库完成什么操作。

真正怎样定位记录、检查条件、修改数据，是 MySQL Server 和存储引擎负责的事情。

这就产生了一个值得继续追问的问题：

**我们只是发送了一段 SQL 文本，MySQL 凭什么知道该怎样执行？**

不过，在进入源码之前，我们还需要先确认一次购买是否真的成功。

## 5. UPDATE 执行成功，不等于一定买到了商品

假设库存还有 `100` 件。

执行：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

如果更新成功，库存会减少一件。

但现在我们修改实验数据，让商品库存变成零：

```sql
UPDATE products
SET stock = 0
WHERE id = 42;
```

然后再次购买：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

这条 SQL 在语法上完全正确。

MySQL 也可以正常处理这个请求。

但商品库存已经是零，不满足：

```sql
stock > 0
```

因此，没有记录需要被这条语句修改。

注意，这里出现了两个不同的问题。

第一个问题是：

> 这条 SQL 有没有执行错误？

第二个问题是：

> 这次购买有没有真正扣减库存？

两者并不等价。

如果 SQL 没有报错，但没有任何记录满足更新条件，就不能把这次操作当作购买成功。

所以，除了检查 SQL 是否报错，我们还需要观察修改了多少条记录。

MySQL 提供了 `ROW_COUNT()` 来获取前一条语句影响的记录数量。

我们可以这样写：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;

SELECT ROW_COUNT() AS affected_rows;
```

如果商品库存为零，预期输出：

```text
+---------------+
| affected_rows |
+---------------+
|             0 |
+---------------+
```

如果先把库存恢复到 `100`：

```sql
UPDATE products
SET stock = 100
WHERE id = 42;
```

然后重新执行购买：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;

SELECT ROW_COUNT() AS affected_rows;
```

预期输出：

```text
+---------------+
| affected_rows |
+---------------+
|             1 |
+---------------+
```

再查询：

```sql
SELECT stock
FROM products
WHERE id = 42;
```

预期看到：

```text
99
```

这里有一个非常重要的细节。

对于普通的 MySQL `UPDATE`，默认的受影响行数通常表示实际发生修改的记录数量，而某些客户端连接选项会使报告值采用匹配行数。

但对于我们当前这条 SQL，条件为 `stock > 0`，而修改是 `stock = stock - 1`。在库存为整数、更新正常完成的场景中，符合条件的记录确实会发生变化。

因此，我们可以把影响一条记录作为本次成功扣减库存的重要判断依据。

现在，我们对最初的购买任务终于有了更精确的定义：

**购买成功不只是 SQL 没有报错，还意味着目标库存记录确实被成功扣减了一次。**

后面研究事务和并发时，我们还会继续完善这个定义。

本章暂时只要求单次购买完成一次正确扣减。

---

## 6. 到这里，MySQL 帮我们隐藏了什么？

我们先比较两种实现。

Python 版本：

```python
product = products.get(42)

if product and product["stock"] > 0:
    product["stock"] -= 1
```

MySQL 版本：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

Python 版本中，每个操作都是我们自己实现的。

我们知道字典如何保存商品。

知道 `.get(42)` 如何获取对象。

也知道 `stock -= 1` 会修改哪个值。

但 MySQL 版本不同。

我们只提供了一段描述目标操作的 SQL。

MySQL 必须自己完成至少以下工作：

```text
接收 UPDATE 请求
        |
        v
理解要修改哪张表
        |
        v
确定需要查找什么记录
        |
        v
读取并检查目标记录
        |
        v
修改 stock 字段
        |
        v
报告执行结果
```

这张图只是从外部可观察行为推导出的最小任务流程，不代表内部源码只有六个函数，也不代表所有步骤严格一一对应某个模块。

现在我们面临一个选择。

可以直接去学习 MySQL 的完整架构。

例如，先介绍 SQL 解析器、优化器、执行器、存储引擎，再介绍 InnoDB 的页面和索引。

但这样做存在一个问题：

我们还不知道这些组件究竟是在什么阶段解决什么问题。

如果现在就把所有组件展示出来，读者很容易陷入记忆名词和函数关系的过程。

因此，我们换一种办法。

**先找到这条 UPDATE 进入 MySQL 的第一个可追踪的源码位置。**

之后再顺着执行过程逐层深入。

## 7. 第一次进入真实 MySQL 源码

本系列的源码研究统一以 MySQL 8.4 LTS 为主线。

本章固定版本如下：

| 项目 | 内容 |
|---|---|
| 源码仓库 | `mysql/mysql-server` |
| 版本 | MySQL 8.4.0 |
| Git Tag | `mysql-8.4.0` |
| 源码文件 | `sql/sql_parse.cc` |
| 当前研究对象 | 普通客户端发送文本 SQL 的请求路径 |

对应源码：

[MySQL 8.4.0 — sql/sql_parse.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_parse.cc)

注意，这里限定的是普通文本 SQL 请求。

通过预处理语句协议执行 SQL，会涉及不同的命令入口。我们先不研究那些路径。

现在，我们的目标只有一个：

**找到客户端发送的 SQL 最初怎样交给 MySQL Server 处理。**

### 7.1 客户端发出的不是一次 C++ 函数调用

当我们在 MySQL 客户端中输入：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

客户端并不是直接调用 InnoDB 的某个库存修改函数。

它需要通过 MySQL 客户端与服务器之间的通信协议，把 SQL 请求发送给服务器。

服务器接收到请求后，才会决定怎样处理。

那么，源码里有没有对应的命令接收入口？

我们打开：

```text
sql/sql_parse.cc
```

搜索：

```cpp
do_command
```

可以找到：

```cpp
bool do_command(THD *thd)
```

这是 MySQL 8.4.0 中真实存在的函数。

它负责从连接读取一个命令，并将其交给后续处理逻辑。

这里第一次出现了 `THD`。

我们暂时只需要把它理解为：MySQL 用来保存当前连接及相关执行上下文信息的对象。

没有必要现在深入它内部的所有字段。

因为当前问题不是线程和连接状态怎样管理，而是 SQL 请求究竟从哪里进入。

### 7.2 服务器怎样取得客户端命令？

在 `do_command()` 中，可以找到下面这行代码。

**真实源码节选：`sql/sql_parse.cc`，`do_command()`**

```cpp
rc = thd->get_protocol()->get_command(&com_data, &command);
```

这行代码非常值得注意。

它从当前连接的协议对象获取命令。

其中：

`thd` 表示当前连接的上下文。

`com_data` 用于保存接收到的命令数据。

`command` 表示本次接收到的命令类型。

这里有一个我们此前没有考虑过的事情：

**MySQL Server 不可能把收到的每个请求都直接当作 UPDATE 处理。**

因为客户端可能发送查询，也可能发送其他协议命令。

所以在解释 SQL 内容之前，服务器需要先取得本次请求的命令类型和相关数据。

对于普通文本 SQL，使用的命令类型是 `COM_QUERY`。

注意，`COM_QUERY` 不等于 SQL 中的 `SELECT`。

`UPDATE`、`INSERT`、`DELETE` 等文本语句也可以通过 `COM_QUERY` 发送。

现在，服务器已经获得了客户端命令。

接下来要交给谁？

在同一个函数中，继续找到：

**真实源码节选：`sql/sql_parse.cc`，`do_command()`**

```cpp
return_value = dispatch_command(thd, &com_data, command);
```

这行代码完成了一次重要的职责交接。

`do_command()` 已经读取了客户端命令。

现在，它把当前连接、命令数据和命令类型传给 `dispatch_command()`。

这说明我们找到了一个真实的请求分发入口。

但此时还没有真正修改库存。

它只是把请求交给后续的处理过程。

### 7.3 文本 SQL 从哪里进入后续处理？

继续打开同一个文件，搜索：

```cpp
dispatch_command
```

进入函数内部。

我们能够找到一个处理 `COM_QUERY` 的分支。

这个分支会为 SQL 文本准备解析所需的数据，然后调用：

**真实源码节选：`sql/sql_parse.cc`，`dispatch_command()` 内的 `COM_QUERY` 分支**

```cpp
dispatch_sql_command(thd, &parser_state);
```

这里再次发生了一次职责交接。

`dispatch_command()` 负责根据客户端命令类型选择处理路径。

当它处理的是普通文本 SQL 时，会进入 `dispatch_sql_command()`。

这一次，输入不再只是一个没有分类的网络命令。

我们已经进入了专门处理 SQL 文本的路径。

再搜索：

```cpp
dispatch_sql_command
```

可以找到对应的函数定义：

```cpp
void dispatch_sql_command(
    THD *thd,
    Parser_state *parser_state)
```

这里 `Parser_state` 代表与 SQL 解析过程有关的状态。

我们暂时不需要解释它的内部结构。

只需要知道，它让后续代码能够处理传入的 SQL 文本。

在这个函数内部，我们可以继续找到：

**真实源码节选：`sql/sql_parse.cc`，`dispatch_sql_command()`**

```cpp
err = parse_sql(thd, parser_state, nullptr);
```

到了这里，我们终于看到了一个关键变化。

之前服务器只是接收和分发客户端命令。

现在，它开始尝试解析 SQL。

而 SQL 只有被正确理解之后，才可能进一步执行具体的数据修改。

因此，我们已经找到下一章应该继续深入的源码入口。

---

## 8. 把刚才的源码连接起来

现在，我们可以把已经确认的几个函数放在一起。

```text
客户端发送 UPDATE
        |
        v
do_command()
        |
        | 获取客户端命令
        v
dispatch_command()
        |
        | 识别 COM_QUERY
        v
dispatch_sql_command()
        |
        | 开始解析 SQL
        v
parse_sql()
```

这张图只表示本章已经确认的普通文本 SQL 处理路径中的关键调用关系。

它并没有展示全部调用步骤，也不是完整的 MySQL Server 执行流程。

但它已经帮助我们回答了一个问题：

**客户端发送的 UPDATE 并不会直接进入 InnoDB 修改库存。**

在此之前，MySQL Server 首先需要接收请求，识别命令类型，并开始理解 SQL 文本。

为什么要有这些步骤？

回到最初的 Python 程序就很容易理解。

在 Python 中，我们已经明确写好了：

```python
product = products.get(42)
product["stock"] -= 1
```

程序不需要猜测我们要执行什么。

查找目标和修改方式都直接写在代码里。

而 MySQL 接收到的是：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

这是一段需要解释的文本。

它必须从文本中理解：

要操作哪张表、修改哪个字段、怎样计算新的字段值，以及什么条件下才允许修改记录。

所以，现在我们真正缺少的能力是：

**怎样把一段 SQL 文本转换成 MySQL 能够继续处理的操作结构？**

这个问题，正是下一章的核心。

---

## 9. 实验：确认这次购买的输入和结果

最后，我们整理一个能够重复执行的最小 MySQL 实验。

后续章节研究源码时，都会围绕同一条 UPDATE 展开。

### 准备数据

```sql
USE mysql_source_lab;

UPDATE products
SET stock = 100
WHERE id = 42;

SELECT stock
FROM products
WHERE id = 42;
```

预期库存：

```text
100
```

### 执行购买

先确保当前实验会话启用了自动提交：

```sql
SET SESSION autocommit = 1;
```

然后执行：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;

SELECT ROW_COUNT() AS affected_rows;
```

预期影响一条记录。

再查询：

```sql
SELECT stock
FROM products
WHERE id = 42;
```

预期得到：

```text
99
```

### 验证数据不依赖当前客户端进程

退出当前 MySQL 客户端，但不要停止 MySQL Server。

随后重新建立一个客户端连接，再执行：

```sql
USE mysql_source_lab;

SELECT stock
FROM products
WHERE id = 42;
```

预期仍然能够看到库存 `99`。

这与最初那个每次启动都重新创建字典的 Python 程序不同。

现在，客户端退出不会使 MySQL Server 中已经提交的商品数据随着客户端内存一起消失。

但要注意，这个实验只验证客户端重新连接后仍能读取已提交的数据。

它不是断电测试，也不能单独证明数据库在所有故障条件下的持久性。

关于数据库真正崩溃时应该怎样恢复，我们会在研究 Redo Log 时再进行专门的分析。

### 实验验证范围

本章的 Python 最小实现已经实际运行，验证了购买成功、商品不存在以及重新初始化后状态恢复初始值的行为。

MySQL 实验给出了可以复制执行的 SQL 和预期结果，但本章没有在真实 MySQL 8.4 实例中执行这些 SQL，因此不声称已经获得了真实服务器的运行输出。

而源码部分已经根据 MySQL 8.4.0 的 `mysql-8.4.0` 标签核对了对应的函数和调用关系。

---

## 10. 回看：我们究竟前进了多少？

这一章，我们从最简单的 Python 程序开始：

```python
products[42]["stock"] -= 1
```

它能够修改库存，但业务数据只存在于当前程序创建的对象中。

当我们需要让不同程序在不同时间访问同一份已保存的数据时，就开始把商品信息交给 MySQL Server 管理。

随后，我们使用：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

完成同一个购买任务。

并且发现，判断购买成功不应该只检查 SQL 是否报错，还需要确认库存记录是否真正发生了修改。

最后，我们第一次进入真实 MySQL 源码，找到了普通文本 SQL 请求的一段明确入口：

```text
do_command
    |
    v
dispatch_command
    |
    v
dispatch_sql_command
    |
    v
parse_sql
```

现在，我们已经不再只是知道 MySQL 能够执行 UPDATE。

我们开始知道这条请求从哪里进入。

更重要的是，已经有了可以继续深入的具体位置。

但还有一件事情没有解释。

当 `parse_sql()` 收到：

```sql
UPDATE products
SET stock = stock - 1
WHERE id = 42 AND stock > 0;
```

它究竟怎样区分 `UPDATE`、`SET` 和 `WHERE`？

怎样知道 `products` 是表名，`stock` 是字段名，而 `42` 是一个用于匹配记录的值？

它又怎样把这些信息交给后面的执行过程？

下一章，我们就从已经找到的 `parse_sql()` 继续向下追踪。

不急着进入 B+ 树，也不急着研究事务。

因为目前，我们甚至还没有看到 MySQL 怎样理解这条 SQL 的含义。

**下一章：从 SQL 文本到执行操作——一条 UPDATE 怎样进入 MySQL？**

---

## 本章源码与参考资料

本章统一使用 MySQL 8.4.0，Git Tag `mysql-8.4.0`。

- [MySQL 8.4.0 — sql/sql_parse.cc](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_parse.cc)
- [do_command() 源码位置](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_parse.cc#L1257)
- [dispatch_command() 源码位置](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_parse.cc#L1646)
- [COM_QUERY 处理分支](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_parse.cc#L1957)
- [dispatch_sql_command() 源码位置](https://github.com/mysql/mysql-server/blob/mysql-8.4.0/sql/sql_parse.cc#L4951)
- [MySQL 8.4 UPDATE 官方文档](https://dev.mysql.com/doc/refman/8.4/en/update.html)
- [MySQL 8.4 ROW_COUNT() 官方文档](https://dev.mysql.com/doc/refman/8.4/en/information-functions.html)

本章仅研究普通文本 SQL 请求的入口，不涉及预处理语句执行协议，也不提前展开存储引擎的内部访问路径。