# 包数据库写入中的中断恢复

2026-11-14，北京时间。按批准顺序使用 Ubuntu 的 Fedora 44 原实例、本机 Debian 13 原实例、Ubuntu 的 Arch generation 1 原实例。三个写入中断点均命中，每个实例只注入一次。三个实例均完成原生恢复和基线对账。Fedora 与 Debian 的正式入口在中断后误判无需恢复。Arch 正式入口保留屏障，原生修复后由正式维护回执入口解除。

| 目标 | 写入与中断 | 中断后恢复及结果 |
|---|---|---|
| Fedora 44 | `txn7d69d054191d`。DNF SQLite WAL 页写成功，偏移 56、写入 4096 字节，文件 4152 字节。SIGINT 后 Unifier 返回 130，原生代理退出 1，客体进程树已清扫。[写入证据](ubuntu/fedora44/write-checkpoint.json) | `rpm --verifydb` 退出 0，`pkg recover` **误报** `no-recovery-needed`，停止态读取器仍拒绝非空 WAL。核对原库、备份主库、WAL、SHM 后，原生 `rpmdb --initdb` 在既有标准 SQLite 库上执行 checkpoint。仅移除已校验本轮未登记载荷，原生、登记与停止态恢复到 154 条。[中断回执](ubuntu/fedora44/result.json) · [恢复回执](ubuntu/fedora-maintenance/result.json) |
| Debian 13 | `txn1fb451d0a201`。dpkg 成功写出并改名 172 字节的 `/var/lib/dpkg/updates/0002`，记录本轮包为 installed。SIGINT 后 Unifier 返回 130，原生代理退出 15，客体进程树已清扫。[写入证据](debian13/write-checkpoint.json) | `dpkg --audit` 退出 0，`pkg recover` **误报** `no-recovery-needed`，停止态读取器仍拒绝编号更新文件。备份主状态和 `0000`、`0001`、`0002`，验证三个日志只涉及本轮包。以空输入调用原生 `dpkg --set-selections` 同步日志，清单对齐后正式卸载测试包，回到 691 条。[中断回执](debian13/result.json) · [恢复回执](debian-maintenance/result.json) |
| Arch | `txn19149fea0e59`。pacman 成功关闭 202 字节的夹具 `desc` 文件，`files` 尚未写出。SIGINT 后 Unifier 返回 130，原生代理退出 15，客体进程树已清扫。[写入证据](ubuntu/arch/write-checkpoint.json) | `pkg recover` 正确拒绝有遗留锁的事务，`pacman -Dk` 复核发现文件清单缺失。以批准方案备份约 333 MiB 可写层与 OCI 元数据，保留锁 inode 11957754 和残缺夹具原件。使用原签名包重装，`pacman -Dk` 退出 0，原生系统 hook 完成，夹具 hook 校验内容摘要。正式 `pkg recover --maintenance-receipt` 返回 `recovered`；正式卸载测试包后回到 140 条。[备份](ubuntu/arch-backup-result.json) · [原生修复](ubuntu/arch-maintenance/native-repair.json) · [正式恢复](ubuntu/arch-maintenance/formal-recovery.json) |

## 收尾与范围

- 所有有效中断均记录了原生写入返回、实际写入长度或文件大小、客体及宿主身份和信号。观测模块只用于本轮夹具。未覆盖其他写入窗口、硬断电或宿主崩溃。
- 三个 CID 均已停止，PID 0，租约和 bridge 为空，登记清单已验证，恢复屏障已清。独立监督记录无资源中止，非目标容器未启动，新增容器 0。中断阶段聚合峰值约 120、393、116 MiB，最大任务数 36、42、36。原生维护前备份保存在两台机器上的私有目录，Arch 原锁与残缺记录继续保留。[执行条件](execution.md) · [Ubuntu 终态](ubuntu/final-state.txt) · [Debian 终态](local-registration-after.json)
- 本轮保留 Fedora 和 Debian 的产品缺陷复现。正式入口需区分原生审计通过与停止态库已 checkpoint，当前产品代码未改。工作树源码摘要和 Git 索引已核对，日常安装仍为 v0.0.6。
- RPM 原生写关闭路径会对 SQLite WAL 执行 checkpoint，RPM 文档说明 `--initdb` 不覆盖已有库。dpkg 的原生读写关库路径会把编号更新同步到主状态文件。测试前保留了原生证据，运行前后以本轮实库结果验证这些行为。[RPM SQLite 源码](https://raw.githubusercontent.com/rpm-software-management/rpm/rpm-6.0.2-release/lib/backend/sqlite.cc) · [RPM 命令手册](https://rpm.org/docs/6.0.x/man/rpmdb.8) · [dpkg 数据库源码](https://raw.githubusercontent.com/guillemj/dpkg/main/lib/dpkg/dbmodify.c)
