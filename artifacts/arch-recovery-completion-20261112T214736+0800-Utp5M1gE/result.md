# Arch 原实例恢复完成回执

2026-11-12，北京时间。用户已单独批准 [正式执行单](review.md)。Ubuntu 原 Arch generation 1 的事务 `txn758c9561ce98` 中断恢复 **PASS**。实际恢复命令走正式 `pkg recover --maintenance-receipt` 解析与服务入口，运行时固定在原测试实例。日常安装 v0.0.6 未改动。

| 项目 | 验收结果 |
|---|---|
| 原生锁 | `/var/lib/pacman/db.lck` 原位重命名为 `/var/lib/pacman/db.lck.unifier-quarantine-txn758c9561ce98`，保留 inode `11957434`，原路径已释放。 |
| 后置动作 | 补做 hwdb、`/usr` 更新时间、证书信任库三项。hwdb 原生查询命中，五个稳定证书产物与隔离试验一致，Java 和 TLS 信任证书集合一致。核验三项已有动作，六项因原客体未运行 systemd 按条件不适用。 |
| 产品调和 | `pacman -Dk` 退出 0。正式入口返回 `recovered`，新回执登记成功，`package_recovery_required=false`，登记状态为 `verified`。历史事务仍是 `failed`，原始退出码、原生输出、旧恢复时间、pacman 日志和原保护中止回执均保留。 |
| 三方清单 | 当前原生、Unifier 登记、停止态读取器均为 140 条，SHA-256 均为 `1ea3594789f9bb9627b1b80bd9787c187e176b5bd1fbb0159fad0a32a418d026`。独立复核使用生命周期锁，存储引用为 `released`。首个独立核对脚本读取了错误的回执字段，在调用库存读取前失败；修正后通过，无保护中止。 |
| 实机保全 | Arch 已停止、租约为空、存储引用已释放。Fedora 同伴未启动，其记录、租约、登记清单摘要不变。远端 5 个 CID 的数量、集合和最终状态均不变，本轮监督、工作单元和客体 scope 均退出。 |
| 资源 | 恢复作业保护未触发。原客体 384 MiB，客体采样峰值约 57 MiB，汇总峰值约 144 MiB、28 任务，宿主最低可用约 117 GiB。 |

原恢复写入和正式调和见 [实机日志](formal-remote.log)，独立停止态校验见 [带锁复核](locked-inventory-r2.log)，最终保全见 [最终状态](final-state.json)。保留 [批准记录](approval.json)、[约 333 MiB 的回退点](remote-window-backup.log) 和 [历史保护中止证据](../arch-recovery-tests-20261112T192232+0800-6CcXwggI/report.md)。

本次关闭原 Arch 升级中断的恢复项。完整 Arch 升级在 384 MiB 下仍无有效通过样本，仓库事务、签名本地包及另外两目标剩余事务仍待单独准入。本轮未重跑完整升级。维护回执入口在开发工作树和受保护实机验证副本中验收，未发布、未部署到日常入口。
