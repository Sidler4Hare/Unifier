# Arch 升级中断恢复续测

2026-11-12（北京时间）。继续 [完整恢复方案](../../docs/Arch升级中断恢复方案-20261112.md) 和 [上一轮实机诊断](../arch-recovery-tests-20261112T192232+0800-6CcXwggI/report.md)。本轮完成恢复屏障修复和两个实机诊断补证。真正恢复写入仍为 BLOCKED。

| 用例 | 结果 | 证据 |
|---|---|---|
| 事务中断、历史误恢复与重复调用 | 恢复屏障保持，保留原事务结论及退出码，数据库审计与提交证据分开。无自动重放。不能证明在提交前停止时按不确定处理 | [补丁](recovery-screen-final.patch)，[回归日志](t1-r2/command.log) |
| 当前工作树全仓 | 1029 项，1024 通过、5 条件跳过，原生退出 0。首轮保留旧三发行版矩阵对 Arch 的一个错误期望，更新对应断言后全仓复验通过，Debian 和 Fedora 断言保持原样 | [首轮日志](t1/command.log)，[复验回执](t1-r2/receipt.json) |
| Arch journal 组归属 | PASS。实机 `/var/log/journal` 为 root、GID 981（`systemd-journal`）、模式 2755，与有效 tmpfiles 规则匹配。上一轮 `pacman -Qkk` 的 GID 差异由客体该规则解释 | [客体结果](guest-result.json) |
| Arch hwdb 单项 | PASS。在原 CID 的 384 MiB 预算下，使用客体原生工具和约 9.19 MiB 原始输入，在私有 tmpfs 替代根编译出 13,884,709 字节数据库。原生退出 0，USB 根集线器查询命中。客体 cgroup 采样峰值约 55 MiB，汇总峰值约 145 MiB、23 任务。未触发资源保护 | [客体结果](guest-result.json)，[实机回执](remote-pilot.log)，[资源与收尾](pilot-postflight.json) |

## 恢复下一阶段

- 客体以 `podman-init` 运行，无已启动的 systemd、udev 控制 socket 或系统总线。原 helper 的 sysctl、daemon reload、标记服务、udev reload、D-Bus reload 分支受 `systemd_live` 或 `udevd_live` 条件限制。正式窗口应再次确认这些条件。hwdb、`/usr` 更新时间和证书信任产物要按各自结果核验，隔离 hwdb 通过不外推完整升级。
- 原生锁 inode `11957434`、原 `txn758c9561ce98`、原始中止回执、pacman 日志、原 hwdb 文件保持一致。Arch 已停止，Fedora 未启动，两目标租约为空。远端 5 个 CID 均为 exited，数量、集合未变；精确监督、工作单元和客体 scope 均退出。私有临时根已删除。
- 锁处置须单独明确批准。本轮没有隔离原生锁，也没有重跑真实 hook。当前修复会拒绝对未证实后置动作完成的旧事务进行普通恢复；正式恢复仍需把经核验的 hook 完成记录纳入调和，再验证原生、产品登记和停止态清单三方一致。没有把隔离试验当成真实恢复凭据。
- 当前改动仅在工作树。Git 索引、既有非本轮文件和日常 v0.0.6 安装入口核验一致。未把补丁部署到远端，远端实验仅使用既有保护器和客体原生工具。

systemd 的替代根和输出位置依据 [systemd-hwdb 手册](https://man.archlinux.org/man/systemd-hwdb.8.en)。
