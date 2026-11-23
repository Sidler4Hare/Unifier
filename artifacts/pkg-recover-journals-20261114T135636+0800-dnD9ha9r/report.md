# Fedora 与 Debian 包数据库写入中断复测

2026-11-14 按 Fedora 44（Ubuntu 测试机）→ Debian 13（本机）在两个既有 generation 1 实例串行复测。每个目标捕获实际原生写入后，向经 PID、starttime、boot ID 和 cgroup 核验的本轮 Unifier 进程发送一次 SIGINT。安装入口返回 130，事务保留 `needs-recovery`，然后使用正式 `pkg recover`。

| 目标 | 命中的写入 | 正式入口 | 恢复后原生、登记、停止态三方一致 | 清理夹具后的状态 |
|---|---|---|---|---|
| Fedora 44 | `dnf5` 对 `/usr/lib/sysimage/rpm/rpmdb.sqlite-wal` 的 `pwrite64`，偏移 56，写入 4096 字节 | `txnd0beef06a49e` 返回 `recovered`，RPM 验库通过，WAL 为 0 字节 | 154 条，`1a10a5c9e4c0bf7cc78dbe4f9dc0d25ada5d918f43b0a1cac21f53a87dec8963` | RPM 库没有登记夹具，清理经摘要核验的夹具文件后停止，基线 154 条 |
| Debian 13 | `dpkg` 原子写入 `/var/lib/dpkg/updates/0002`，172 字节 | `txn9d90a107461b` 返回 `recovered`，dpkg 审计通过，`updates` 为空 | 692 条，`472671249a309f00370afcb51e0159595f59d954f196ec5ec0ffb46ae4884573` | 正式卸载已登记的夹具，停止并回到 691 条基线，`74f651784f02216d9b94911b9e1c9d58bec439291358cdf530c065b983af847b` |

原生查询文本单独保存并校验摘要。两次恢复均记录探测 `pending`、原生处理 `clean`、复探 `clean`，再核对停止态清单并清除恢复屏障。

- Fedora [写入点](ubuntu/fedora44/write-checkpoint.json)、[信号](ubuntu/fedora44/signal-sent.json)、[中断事务](ubuntu/fedora44/interrupted-transaction.json)、[正式恢复](ubuntu/fedora44/formal-recovery.json)、[三方清单](ubuntu/fedora44/three-way.json)、[保护与停止态回执](ubuntu/fedora44/receipt.json)。
- Debian [写入点](debian13/write-checkpoint.json)、[信号](debian13/signal-sent.json)、[中断事务](debian13/interrupted-transaction.json)、[正式恢复](debian13/formal-recovery.json)、[三方清单](debian13/three-way.json)、[保护与停止态回执](debian13/receipt.json)。

Fedora 和 Debian 的监督单元分别运行 44 秒与 66 秒，最低宿主可用内存分别约 34.8 GiB 和 2.62 GiB，均高于 1 GiB 保留线。保护器无中止原因，两目标退出后 `Pid=0`，测试工作单元 `MainPID=0` 且无剩余 cgroup。非目标实例的前后生命周期状态及同伴登记一致，活动进程事件均属于当前目标。

## 开发树修复与验收范围

中断后若原生数据库仍有待处理日志，Unifier 保留屏障。正式恢复使用 RPM 自身的 SQLite checkpoint，或 dpkg 原生数据库写回合入编号更新日志。审计拒绝、日志处理失败或停止态清单不匹配时保留屏障。加入针对历史误判、日志处理失败与停止态摘要不匹配的离线回归。[RPM SQLite 后端](https://raw.githubusercontent.com/rpm-software-management/rpm/rpm-6.0.2-release/lib/backend/sqlite.cc) · [dpkg 数据库更新实现](https://raw.githubusercontent.com/guillemj/dpkg/main/lib/dpkg/dbmodify.c)

定向 87 项通过。全仓 T1 共运行 1070 项，1065 通过，5 条件跳过。[定向回执](t1-targeted/receipt.json) · [全仓回执](t1-full/receipt.json) · [全仓日志](t1-full/command.log)

实机使用通过全仓回归并校验摘要的开发树源码。日常安装未部署，Git HEAD 与索引核对一致。此次实证限于表中两个写入点，同轮未另做宿主崩溃或半配置包修复测试。
