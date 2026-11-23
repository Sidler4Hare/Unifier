# Arch 升级中断恢复专项回执

2026-11-12（北京时间）。依照 [当天的完整恢复方案](../../docs/Arch升级中断恢复方案-20261112.md) 执行。整体 **PARTIAL**。Ubuntu 原管理根内的 Arch generation 1（CID `aa38e5466b9665765aefe9931f5550eec78631bfa820121469391845cef7e430`，UUID `5b2111b6-f07e-4a95-9791-8a208343ba13`）已完成受保护的只读诊断。实际恢复仍被屏障阻断。

| 阶段 | 本轮结果 | 证据 |
|---|---|---|
| T1 恢复与日志契约 | 36 项，34 通过、2 失败，子命令退出 1，无保护中止 | [用例和堆栈](t1/command.log)，[保护回执](t1/receipt.json)，[离线复现](recovery_probe.py) |
| 原实例和同伴核验 | Arch 和 Fedora 身份、配置、镜像、挂载及限额匹配。两边开始均已停止、PID 0、租约为空 | [远端预检](remote-preflight-r2.json) |
| 回退点准备 | 约 333 MiB 可写层与容器 userdata 已在私有目录完成 checksum、硬链接、设备、ACL、xattr 和权限比较。相关管理文件及 Podman 精确配置行留存 | [体积](remote-size-r2.log)，[回退点](remote-rollback-r4.log) |
| 受保护原生诊断 | 子命令退出 0，保护未触发。客体 384 MiB，汇总峰值约 103 MiB、32 任务，Ubuntu 可用内存最低约 117 GiB | [实机回执](remote-native-diagnostic.log)，[诊断证据](native-evidence.json) |
| 真正恢复及后续事务 | BLOCKED。原生锁和产品恢复屏障保留。未重放 hooks、再次升级或改包 | [诊断证据](native-evidence.json) |

## 两个可复现的恢复缺口

- 模拟已写入包库、进入第 4 个 hook 后退出 137，且原生锁已不存在。当前收尾将事务写成 `failed`，没有保持 `needs-recovery`。
- 模拟旧 journal 已记为 recovered，但当前环境仍标未知，且 hook 完成情况未确认。当前 `pkg recover` 再次返回 `recovered`。锁仍存在时的拒绝、事务前退出的原生退出码保留及无自动重放等用例通过。

本轮只使用 fake runtime 复现这两项，未运行真实的锁处置，未修改生产代码。下一次普通恢复不得凭数据库审计和锁缺失推断后置动作完成。

## 原生数据库、文件和 hook

- `pacman -Dk` 退出 0，数据库格式为 9。原生 `pacman -Q` 为 140 条，与登记清单 SHA-256 一致。24 个变更包的 `-Qk` 退出 0，没有报告缺失包文件。详细 `-Qkk` 退出 1。缺失的文档、man、info 目录及保留的 `pacman.conf` 与镜像的 `NoExtract` 配置相符。另有 `/var/log/journal` 的 GID 差异，未证明是预期的 tmpfiles 或用户命名空间结果。[原生差异](native-evidence.json) · [物理层核对](physical-followup-r2.json)
- 本轮读到第 4 项的 `hwdb.bin` 仍为 11 月 3 日 13:47 的版本，而 catalog 数据库写于原中断当日 15:22:47。这与原日志停在更新 hwdb 的证据一致。现有包和文件列表、当前 hook 定义及旧成功事务都指向 12 个后置候选项，当前这些定义均无 `NeedsTargets`。候选推导仍须结合事务前路径状态和各动作的适用条件，不能凭本轮没有报错认定后续动作执行过。[候选及产物](native-evidence.json) · [ALPM hook 手册](https://man.archlinux.org/man/alpm-hooks.5.en)
- 原中止前，客体采样峰值约 368 MiB，其中 file 约 259 MiB、anon 约 79 MiB。旧回执另记录 28 次硬限额事件。采样无法把用量归到某个 hook 进程，也无法证明原预算能完成 hwdb。[历史采样提取](prior-guest-pressure.json)

## 收尾与下一阶段门槛

远端五个既有 CID 的最终状态均为 exited，数量和集合未变；Arch 和 Fedora 的租约、登记文件、库存文件及原 Arch 事务摘要均保持一致。Arch 原生锁 inode `11957434`、pacman 日志和原中止回执保留，Arch 仍为 `package_recovery_required=true`、库存状态 `unknown`。监督与工作单元 MainPID 为 0，工作 cgroup 已退出，未启用显示、GUI 或会话转发。Fedora 的实库在本轮未挂载重读；核对的是停止状态和原登记库存文件。

单独 tar 归档无法完整保留四个 GPG socket 和部分 overlay 设备硬链接，保留原失败比较。经过 rsync 补齐、私有目录复核的树和对应元数据才是本轮回退材料。未对活容器执行回退或从副本引导。回退材料保留在远端 `/home/agent/unifier-package-closure-20261112/arch-rollback-20261112-3gnqJtrg/restore-rehearsal`，对应管理元数据和回执在同前缀的 `arch-rollback-20261112-e8SqWtUb`。

工作树源码哈希、Git 索引、日常 v0.0.6 入口及本机桌面 PID 核对一致。恢复写入前仍需确认 journal 目录组归属和 hook 适用性，证明 hwdb 可在现有预算完成，并处理两个恢复屏障回归。达到条件后再按方案提交精确的锁处置及 hook 执行清单供单独批准。
