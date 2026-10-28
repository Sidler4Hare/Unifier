# 投影三处语义缺口修复与真实验收

日期：2026-10-27，Asia/Shanghai。**结论：上轮确认的三个问题已修复，并通过本机真实 FUSE 与 Debian/Fedora/Arch 完整产品链路复验。** 全量 411 项（407 通过、4 条件跳过），真实 FUSE 核心 22/22、新边界 9/9，三客体各 26/26，共 78/78。机器结论见 [acceptance-result.json](acceptance-result.json)。

这是 Fedora 44 单宿主的受限文件语义验收；外部 watcher、性能目标、通用 POSIX/IDE 及跨宿主验收仍开放。默认挂载点首次撤销仍可能遇到外部句柄，见下文，不宣称即时撤销全部通过。

## 备份与施工范围

施工前完整备份：`/home/omg/文档/Unifier-backups/20261027-102659-before-projection-boundaries/`。33,346 条目逐项核验；archive SHA256：`6c57be668101cc11fd63788f7b206ef2a3a3c4507d3824f418806e0d589f6da5`。5 个历史 socket 节点以元数据和恢复脚本保留，恢复配方已实际检查；不声称保存活进程/socket 会话。[备份核验](backup-verification.json)。10 月 26 日备份、legacy 实现与既有失败证据保留。

沿用 dsh Command Code / `deepseek/deepseek-v4.1-flash` 施工，主控独立审查、补修和执行。主控在方案纠偏及 405 项施工版回归后接管；两段 CLI 因主动 SIGINT 以 130 退出，原日志保存在 `.lab/agents/projection-boundaries-20261027/`，不把 CLI 状态当作验收结果。最终以冻结源码、独立探针及清理核验为准。

产品仅修改 `projection.py`、`view_backend.py`、`fuse_view.py`，补充相关回归测试和本轮说明。控制层、主状态、日常 FreeCAD 部署未调整；既有未提交工作保留，未提交/推送。

## 三个修复及实际结果

| 问题 | 最终行为 | 真实挂载证据 |
| --- | --- | --- |
| 同大小原地修改并恢复旧 mtime，旧 FD 读到旧页 | 观察真实 size/mtime/ctime；属性重验证发现变化后先失效页，再回复真实属性。旧 FD、新 open 均读新内容 | 新旧 SHA 均为 `7824a27e…e7e0b`；mtime 保留、ctime 改变 |
| 中间 symlink 与 `..` 被词法折叠，读错目标 | 按组件处理链接、目录和 `..`，在投影根钳制向上解析 | `jump/../target` 的原生与投影均读 `ROOT-CORRECT` |
| RELEASEDIR 后节点回收，随后 stat 的 inode 改变 | 稳定对外 inode 与可回收 FUSE nodeid 分开；由 view/provider/mount/dev/ino 身份计算 | a/b/c 的列出值与随后 stat 全部一致；65 条目反复关闭目录 5 轮映射不变 |

直接挂载详情：[kernel-boundaries-final-v2/result.json](kernel-boundaries-final-v2/result.json)；三客体产品详情：[broker-final-v2/result.json](broker-final-v2/result.json)。修复前同一组探针 2/9 通过、7/9 失败，包含原三个反例，见 [baseline-boundaries-v2/result.json](baseline-boundaries-v2/result.json)。原 10 月 27 日复测结果也保持原样。

### 内容失效与引用回收

保留 cached I/O 和只读 mmap，entry/attr TTL 仍为 0，继续协商 AUTO_INVAL_DATA。新增正确的 `FUSE_NOTIFY_INVAL_INODE` 帧；不伪造 mtime/ctime。除原例外，连续 4 次恢复 mtime 的原地写入，在不显式 fstat 的 pread 下也正确；已映射页面经 fstat 重验证后连续 3 次正确刷新。

属性版本放在 Node 上，随 FORGET/open/internal 引用一起回收；没有独立、无限增长的历史版本字典。新增回归覆盖子节点已 FORGET、由目录快照暂时持有、最后 RELEASEDIR 收集的顺序。真正的通知写入失败明确报错并保留待重试版本；未成功回复的 LOOKUP 回滚引用。

最终重跑前发现并修正了高频替换时的一处竞态：内核 inode 已回收、用户态 FORGET 尚未处理时，页失效通知可能返回 ENOENT。上游 `fuse_reverse_inval_inode` 在找不到内核 inode 时返回该值，此时没有旧缓存需要清理；与真正 I/O 失败分开处理，其他错误仍明确失败。[内核实现](https://raw.githubusercontent.com/torvalds/linux/master/fs/fuse/inode.c)、[协议参考](protocol-references.json)。最终三客体观察到 2/0/2 次此类 absence、真正通知失败均为 0，250 次并发打开读取各轮通过。

这仅是 **on-revalidation** 内容失效，不是后端主动监听、持续 mmap 自动刷新或修改事件桥。最终真实探针仍观察到投影侧 inotify 无外部修改事件，状态仍明确 `watcher=false`。依赖本机实际提供的 AUTO_INVAL_DATA；其他内核/后端时间粒度未外推。

### 链接解析与拒绝边界

同时验证链接链、缺失前缀 ENOENT、非目录前缀 ENOTDIR、循环 ELOOP、末尾 `/` 和 `/.`、中文/空格，以及跨 provider 的组件解析。根钳制探针和“先取得悬空链接文本、再创建中间目录”的宿主 sentinel 探针都未越过投影根。

普通悬空后缀仍可显示。若缺失/非目录中间组件后还有未解析 `..`，投影 READLINK 明确返回对应错误，避免导出可在目录变化后越界的文本，也不靠删除 `..` 伪造另一个目标。原始 `readlink_raw` 不改变文本。相对硬链接 symlink 缺少别名上下文时的 EOPNOTSUPP 策略保留。

### 稳定 inode 与资源上限

对外 inode 使用带 view 身份域的 64 位 BLAKE2 值，根为 1；FUSE nodeid 仍独立、单调且不复用。只保存活跃 inode 碰撞表，随节点回收清理，不保留所有历史路径/锚点。稳定性限于同一 view 和底层对象身份；不承诺跨 view 编号，也保留底层 inode 复用边界。

64 位摘要存在理论碰撞；同时存活的不同身份碰撞会明确 EOVERFLOW，不静默混同。注入 10 次碰撞拒绝后 FD 保持 **7→7**，nodes/inode_ids 回到 1。[碰撞回滚](collision-final-v2.json)。小节点预算 4 下经过 64 次其他对象创建/查询/回收，原条目编号不变，nodes/inode_ids 仍为 1。[小预算稳定性](identity-final-v2.json)。

## 最终验证

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| 全量 unittest | 411 总计，407 通过、4 条件跳过 | [日志](unittest-final-v2.log) |
| 相关协议、对象、目录、失败边界回归 | 66/66 | [日志](unittest-focused-final-v3.log) |
| 原有真实 FUSE 核心用例 | 22/22 | [结果](kernel-core-final-v2/result.json) |
| 新增真实 FUSE 边界组 | 9/9 | [结果](kernel-boundaries-final-v2/result.json) |
| Debian 13 / Fedora 44 / Arch 完整链路 | 各 26/26，共 78/78 | [结果](broker-final-v2/result.json) |
| 旧预算/失败路径契约 | 7/7 | [结果](contracts-final.json) |
| 小预算稳定性、碰撞拒绝回滚 | 2/2 | [稳定性](identity-final-v2.json)、[回滚](collision-final-v2.json) |
| 保全、源码匹配与测试资源退役 | 通过 | [审计](audit.json)、[冻结源码](source-frozen-v2.json) |

4 项条件跳过仍为 GUI、显式启用的 rootless 生命周期、安装及资源集成模块。本轮另行执行真实 FUSE/broker，不把 skip 算通过，也没有宣称重做 GUI/IDE、安装/保存/重开、性能矩阵或跨宿主验收。三发行版均是同一个 Fedora 44 宿主上的客体。

保留原子替换的 32→128、128→32、同大小、已读/未读 FD，unlink/recreate、硬链接、mmap、250 次并发打开、目录分页/rename、provider 覆盖、只读/权限和资源耗尽恢复等原有核心行为。三客体均验证活动 view 阻止 start/remove、持 FD 阻止撤销、关闭后正常撤销、租约归零和精确 CID 移除。

## 失败历史与默认挂载点生命周期

- 首稿有限 LRU 身份缓存被否决：小预算 churn 后原 inode **2→67**。失败记录见 [identity-attempt1.json](identity-attempt1.json)。
- 中间版纯身份映射已稳定，但碰撞错误分支 FD **7→17**、未解析链接后缀可能越界；源码和反例保留，最终修正。[中间审查](controller-review.md)、[kernel-attempt2](kernel-attempt2/result.json)、[碰撞反例](collision-attempt2.json)。
- 第一轮最终挂载的新 9 项已通过，但旧并发替换出现前述 ENOENT：直接核心 21/22，客体 Debian 26/26、Fedora/Arch 25/26。该轮与源码保留，未改写成绿色。[核心失败轮](kernel-core-final/result.json)、[客体失败轮](broker-final/result.json)。修正后使用新的 `*-final-v2` 目录完整重跑。

默认工作区挂载点另做 5 轮：持 FD 时均正确拒绝撤销；关闭后首次正常撤销 **4/5**，最终正常清理 **5/5**。第 2 轮出现约 1.89 秒 EBUSY，现场 worker 有 3 个目录句柄，fuser 显示 ChatGPT 进程持有挂载内文件。随后正常重试成功，没有 lazy unmount、强行关闭外部句柄或杀进程。[生命周期记录](default-lifecycle-final/result.json)、[现场归因记录](default-lifecycle-attribution.json)。

这些证据支持本次存在外部访问者，不能证明以往所有 EBUSY 的原因，也不能宣布“关闭自己的 FD 后一定立即撤销”。该开放项与本轮三个文件语义修复分开记录。

## 保全、恢复与复现

最终检查 22,599 个原有文件，除本轮允许的源码/测试/README/HANDOFF 外无意外改动；23 个主状态文件、21,982 个历史证据文件及原有文件类型/模式/链接目标保留。27 原容器身份、配置、最终状态和运行时间字段均与本轮基线一致；本轮没有启动日常 FreeCAD。测试容器、view、租约和 worker/broker 均退役，无 Unifier FUSE/storage 挂载残留。

最终源码在 `accepted-source-v2/`，与当前源码和实际三客体使用的 `broker-final-v2/project/src/` 哈希一致。`accepted-source/` 和 `source-frozen.json` 保留第一轮失败源码；不会用后来实现替换旧证据的源码路径。

从仓库根执行，输出目录必须尚不存在；脚本使用独立状态和新 fixture，不覆盖旧轮次：

```bash
python3 evidence/projection-boundaries-2026-10-27/real_boundary_probe.py --source src --out /tmp/unifier-boundary-new
python3 evidence/projection-boundaries-2026-10-27/real_broker_boundaries.py --out /tmp/unifier-broker-boundary-new
```

恢复应从外部备份解压到独立目录核对，不覆盖当前工作树或清理旧实验。实现与边界见[设计说明](../../设计/投影边界修复-2026-10-27.md)。外部通知/丢事件策略、真正快照、多卷协调、性能/长压、复杂 ACL/xattr、GUI/IDE 和跨宿主仍开放；普通项目继续直接共享数据根/bind。
