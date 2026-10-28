# 文件投影系统复测（2026-10-27）

**结论：上轮的核心修复得到复核，但文件语义尚未全部解决。** 原子替换/unlink 后已打开 FD 的对象身份、常规大小/内容变化、目录分页与引用回收继续通过；扩展输入发现 3 处语义缺口，已知的外部修改通知仍缺失。不能把旧用例全绿解释为通用 POSIX 语义通过或 F 阶段完成。

机器结论见 [retest-result.json](retest-result.json)。本轮只测试、增加复现脚本并更新 README/HANDOFF，没有修改产品源码、测试目录、工具或配置，没有提交/推送。当前源码与 [10 月 26 日已验收版本](../projection-objects-2026-10-26/report.md)哈希一致；当时的外部备份和 legacy worker 保留。

## 覆盖范围与已通过部分

全部发生在同一 Fedora 44 宿主：内核 `7.2.11-200.fc44.x86_64`、Podman `5.8.9`。Debian 13/Fedora 44/Arch 是客体，不能外推为三个宿主发行版验收。使用独立源码快照、私有状态目录、新临时容器和真实内核 FUSE 挂载；主 `.lab/state` 未用作测试状态。环境信息见 [host.json](host.json)、[baseline.json](baseline.json)。

| 验证 | 本轮结果 | 原始证据 |
| --- | --- | --- |
| 全量 unittest | 399 项：395 通过、4 条件跳过 | [unittest.log](unittest.log) |
| 直接真实 FUSE 核心语义 | 22/22 | [kernel/result.json](kernel/result.json) |
| 完整 broker → namespace/storage → FUSE 链路 | 三客体各 17/17，共 51/51 | [broker/result.json](broker/result.json) |
| 预算、失败路径、errno 等补充契约 | 7/7 | [contracts.json](contracts.json) |
| 默认工作区挂载点的撤销生命周期 | 连续 5 轮关闭 FD 后首次正常撤销均成功 | [default-lifecycle/result.json](default-lifecycle/result.json) |

四项跳过涉及 GUI、显式启用的 rootless 生命周期、安装和资源集成测试；本轮没有重新完成 GUI/IDE、安装/保存/重开验收。

核心用例覆盖：32→128、128→32、同大小替换的缓存预热/未预热情况；旧 FD 保持旧对象、新 open 读取新对象；unlink 后旧 FD 可读及同路径重建；常规原地更新（mtime 改变）；mmap 的旧/新对象隔离；并发替换；普通硬链接；provider 身份隔离；目录改名后的旧句柄；覆盖图、缺失祖先与目录分页；只读/权限拒绝；FORGET/RELEASEDIR、预算失败回滚和真实 FD 耗尽恢复。

上轮出现过一次关闭 FD 后立即 revoke 仍 EBUSY。本轮默认挂载点 5/5 未复现，持 FD 时 5/5 正确拒绝撤销。**历史故障原因仍未证明**，不以有限重复宣布永久消除；原始失败与正常重试记录不改写。

## 尚未解决的语义

直接挂载的三次独立 fixture 都得到同样结果；其中两轮同时执行，但挂载与后端相互隔离。随后使用新的三客体完整 broker 链路再次验证：每客体原 17 项通过，新增 4 项全部不满足原生对照（共 12 个失败），无探针异常，正常撤销与容器清理成功。

直接证据：[extra/result.json](extra/result.json)、[extra-repeat1/result.json](extra-repeat1/result.json)、[extra-repeat2/result.json](extra-repeat2/result.json)。产品链路证据：[boundary-broker/result.json](boundary-broker/result.json)。

| 输入/操作 | 原生路径 | 投影路径 | 分类 |
| --- | --- | --- | --- |
| 已读缓存的 FD；4096 字节原地改写，恢复原 mtime，fstat 后再次 pread | 读取新 `N` 内容 | 旧 FD 仍读取旧 `O`；重新 open 正确 | 本轮新增确认：内容新鲜度缺口 |
| `a/jump -> ../b`，`a/link -> jump/../target` | `ROOT-CORRECT` | `A-WRONG` | 本轮新增确认：链接解析到错误文件 |
| scandir 完成并关闭目录，再 lstat 未变化的条目 | 列出 inode 与 stat 一致 | 例如 a 为 10→13，b 为 11→14，c 为 12→15 | 本轮新增确认：回收后的 inode 稳定性缺口 |
| 对后端原地写入与原子替换，监听投影目录 1 秒 | 收到 7 个相关 inotify 事件 | 未收到事件；新 open 内容正确 | 已知未实现：外部修改通知 |

### 1. 保留 mtime 的原地修改仍可读到旧页

同一后端 inode 原地从 `O*4096` 改成 `N*4096`，fsync 后恢复旧 mtime。实际 ctime 已变化，大小仍为 4096。投影旧 FD 在 fstat 后读取的 SHA256 仍为 `957269f1…91dfa6`，原生 FD 为 `7824a27e…e7e0b`；重新打开投影可见新内容。

这与已修复的“原子替换后旧 FD 被错误切换到新对象”不同：此处对象没换，应体现 live 文件更新。当前 cached I/O、AUTO_INVAL_DATA 与 TTL 0 仍不足以覆盖该输入。代码位置为 [fuse_view.py](../../src/multilinux/fuse_view.py#L138) 的缓存/属性策略及 INIT 协商。将结果归因于 size/mtime 不变时未失效是机制推断，本轮未做内核事件跟踪，不宣称已证明全部内核因果。

### 2. 词法折叠 `..` 改变符号链接语义

`jump/..` 必须先跟随 `jump`，再取实际目录的父目录。当前 [view_backend.py:400](../../src/multilinux/view_backend.py#L400) 的 `projected_link()` 使用 `posixpath.normpath()` 折叠文本，直接去掉 `jump/..`，从而读到 `a/target` 的错误内容。

这是普通单一链接的反例；与上轮对“相对 symlink 硬链接别名上下文不明确时 EOPNOTSUPP”的保守策略不同。后续应保持逐组件解析语义及投影边界，不能把错误目标当作成功。

### 3. 目录回收后的 `d_ino/st_ino` 不一致

探针在没有外部改名/替换的目录中保留 scandir 结果，关闭目录并等待 worker `dir_handles=0`，随后 lstat 文件。快照引用释放后，无 lookup/open 引用的节点可被回收；下一次 lookup 分配新 nodeid，并用作 inode 值，因此与刚列出的值不同。

相关位置：[projection.py:378](../../src/multilinux/projection.py#L378) 的分配、[release:677](../../src/multilinux/projection.py#L677) 与 [_collect:947](../../src/multilinux/projection.py#L947)。旧 `dirent-identity` 测试证明存活引用范围内一致，本轮补充了 RELEASEDIR 后的边界。**不能靠永久禁止回收解决**；后续需把对外 inode 身份与可回收 node/anchor 生命周期协调起来。

### 4. 外部修改通知仍未实现

worker 自报 `watcher=false`，因此这不是已宣称能力的回归。但实际监听原生有事件、投影无事件，说明编辑器/索引器依赖的自动刷新关口仍未通过。读取新内容、FUSE 属性失效和传递 inotify 语义是不同验收项。

首次 `extra` 脚本以退出 0 表示“观测执行完成”，其 JSON 内的语义比较已经是 false；这不算通过。后续脚本增加明确的 `native_equivalence_passed=false` 并以退出 1 表示语义未通过。原始结果保留，未覆盖成绿色。

## 有限性能复测

每负载 5 对 native/FUSE 样本，顺序交替，核对相同字节/哈希；不主动清空系统缓存。数据后端是 `/tmp` tmpfs，CPU governor 为 powersave，日常桌面仍运行。表中为 p50（毫秒），不是冷盘吞吐、完整 broker 性能矩阵或 SLO 验收。

| 负载 | 当前原生 | 当前 FUSE | 当前/原生 | 修复前 FUSE 对照 |
| --- | ---: | ---: | ---: | ---: |
| stat 2000 文件 | 31.46 | 1610.67 | 51.20× | 31.18 |
| 读取并哈希 2000×4KiB | 59.26 | 2078.70 | 35.08× | 1278.09 |
| 读取并哈希 16MiB | 29.00 | 52.74 | 1.82× | 710.30 |

当前样本为 09:56:34–09:56:54，[performance-isolated/result.json](performance-isolated/result.json)；修复前源树对照为 10:09:27–10:09:38，[performance-legacy-confirmed/result.json](performance-legacy-confirmed/result.json)。修复前各负载自己的原生 p50 分别为 30.72、60.68、27.77 ms。对照源为 `.lab/agents/projection-objects-20261026/baseline/src/`，源码清单见机器结果。

本轮观测显示顺序读明显改善，小文件路径仍重。旧版 attr/entry TTL 为 1 秒，新版为 0；旧版 stat 首轮约 441 ms，后续主要命中缓存，不能把较低 p50 当作同等一致性下的优势。本轮没有拆分各机制耗时，也不直接与旧 Stage F 的 21–43× 比率作趋势比较。

早先 09:58:47–09:58:58 的 [performance-legacy/result.json](performance-legacy/result.json) 与日常 FreeCAD 启停重叠，因此保留但不用作表中主要旧版对照；最初 `extra` 内性能与本轮其他探针并发，也不作主要数字。上述最终当前样本及补跑旧版均在该 FreeCAD 活动区间之外，仍不等于受控空闲主机测量。

## 保全与清理

[audit.json](audit.json) 核对 22,198 个原有文件：产品源码、23 个主状态文件、21,581 个历史证据文件哈希保留；仅 README/HANDOFF 因本轮报告更新而改变。所有测试 view、精确测试容器及租约已清理，没有本轮 worker/broker、Unifier FUSE 或 rootless storage 挂载残留。原有 27 个容器身份、配置与最终状态保留。

日常容器 `ml-53c5c8a5-freecad-arch` 在 09:58:24–09:59:19 启动并退出，StartedAt/ExitedAt 相对测试前发生变化。用户确认“是，我或其他聊天启动的”。该变化按外部活动记录，不回写时间戳，不声称运行时完全未变。原始首次审计未通过结果保存在 [audit-before-activity-confirmation.json](audit-before-activity-confirmation.json)；[事件](daily-container-events.jsonl)和[用户确认](runtime-activity-confirmation.json)保留。最终审计状态为 `PRESERVED_WITH_USER_CONFIRMED_RUNTIME_ACTIVITY`。

## 复现入口与后续优先级

从仓库根目录执行，输出路径必须尚不存在；这些脚本只用于独立复验，不写主状态。边界脚本在当前源码下预期退出 1，并保存失败比较及清理结果。

```bash
python3 evidence/projection-retest-2026-10-27/extra_probe.py --source src --out /tmp/unifier-retest-new --skip-benchmark
python3 evidence/projection-retest-2026-10-27/boundary_broker.py --out /tmp/unifier-boundary-new
python3 evidence/projection-retest-2026-10-27/lifecycle_probe.py --out /tmp/unifier-lifecycle-new
python3 evidence/projection-retest-2026-10-27/performance_probe.py --source src --out /tmp/unifier-performance-new
```

本轮没有修这些新反例。下一轮先处理错误链接目标与保留 mtime 时的内容失效，再协调稳定 inode 和资源回收；通知与事件丢失策略单独验收。保留原子替换、mmap、目录分页和预算回滚的已有成功行为，再用上述反例及三客体真实挂载复验。严格快照、多卷协调、复杂 ACL/xattr、长时压力、GUI/IDE、跨宿主仍开放。
