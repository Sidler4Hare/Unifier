# Arch 正式恢复执行单

适用 Ubuntu 现有 Arch generation 1、UUID `5b2111b6-f07e-4a95-9791-8a208343ba13`、完整 CID `aa38e5466b9665765aefe9931f5550eec78631bfa820121469391845cef7e430`、原事务 `txn758c9561ce98`。正式阶段需要按 [恢复方案](../../docs/Arch升级中断恢复方案-20261112.md) 单独明确批准锁处置。本执行单未启动正式写入。

- 新鲜回退点已经在私有目录以文件校验和、权限、UID/GID、硬链接、设备、ACL 和 xattr 复核。原可写层与容器 userdata 约 333 MiB，保留精确容器配置和管理记录。未从回退副本引导。[回退点回执](remote-window-backup.log)
- 本机全仓 1033 项，1028 通过、5 条件跳过、原生退出 0。新增正式 `pkg recover ID --maintenance-receipt PATH` 入口。校验精确实例、覆盖的中断日志摘要、原生锁、数据库审计和当前原生清单，保存回执后才清除屏障。Hook 完成由受保护执行器采集并由维护者提交，产品入口不会自行重放。[回归回执](t1/receipt.json) · [补丁](full-recovery.patch)
- 原生 hwdb 和证书信任库的隔离生成已通过。证书试验两次生成，五个稳定输出逐字节一致，Java 文件随条目时间变化，验收改为核对 Java 和 TLS 的 X.509 证书集合。现有回退点对照两边均为 118 个证书。[证书实验](ca-pilot-result.json) · [Java 与 TLS 对照](backup-jks.json) · [p11-kit JKS 生成器](https://github.com/p11-glue/p11-kit/blob/master/trust/extract-jks.c)

## 待批准的精确动作

1. 独占原管理根包协调锁及 Arch 包和生命周期锁，复核无租约和外部写者，确认原包事务、CID、原生锁元数据、客体脚本和回退点身份。任一不符就停止。
2. 只在该 Arch 实例内，把 inode `11957434` 的 `/var/lib/pacman/db.lck` 原位重命名为 `/var/lib/pacman/db.lck.unifier-quarantine-txn758c9561ce98`。禁止覆盖同名目标，保留原 inode。
3. 执行三个客体维护动作，分别为原 hwdb hook、`/usr` 更新时间、证书信任库重建。证书原生脚本会刷新该客体的 CA bundle 和 `/etc/ssl/certs` 下由它管理的符号链接。原日志推进到 hwdb 前的三个动作保留原证据。六个依赖已启动 systemd 的动作按原 helper 条件核验不适用。若 systemd、udev 或系统总线条件发生变化，停止并复核。
4. 由正式 `pkg recover --maintenance-receipt` 对账，验证原生、登记及停止态读取的 140 条清单一致，保留原始中止记录、隔离锁和回退副本。任一阶段失败保留现场和屏障，停止后续写入。Fedora 同伴保持停止。

沿用原客体 384 MiB/128 任务，汇总 1 GiB/256 任务，独立监督，单次 120 秒。零新增容器。批准范围只到该 Arch 中断的恢复与三方清单验收。完整升级和其他发行版事务属于后续用例。[逐项执行规格](maintenance-plan.json)
