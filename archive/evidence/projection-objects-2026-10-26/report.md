# 投影对象、目录与回收修复验收

日期：2026-10-26T22:59:02+08:00（Asia/Shanghai）。结论：**本机只读投影的普通文件对象、大小/内容、目录与引用回收修复通过受限验收**。机器状态为 `LOCAL_READONLY_OBJECT_DIRECTORY_REPAIR_ACCEPTED_WITH_OPEN_GATES`。F 全阶段、通用 POSIX/IDE 文件体验仍未全部通过。

本轮沿用 dsh Command Code / `deepseek/deepseek-v4.1-flash` 施工，由主控独立审查、补反例、纠错和实际执行。最终结论依赖冻结源码和真实内核挂载，不依赖模型完成消息。现有未提交工作保留，未提交/推送。

## 备份、范围与恢复

施工前完整保存项目（含 Git、未提交/未跟踪文件与忽略目录），逐项核验 **31,529 条目**。备份位于 `/home/omg/文档/Unifier-backups/20261026-220929-before-projection-objects`，压缩包 SHA-256 为 `733fe9bfe8a5cd0dc024e2418521430e3296300cfc19d21e3493cf335eb54865`；本次收尾再次复核压缩包哈希。[备份核验](backup-verification.json)。

旧 backend 与备份逐字节一致；旧 worker 除导入旧 backend 外与备份相同。它们保留在 `src/multilinux/view_backend_legacy.py`、`src/multilinux/fuse_view_legacy.py`，可用 `python3 -m multilinux.fuse_view_legacy` 加原 worker 参数单独启动。原架构评审、F 失败结果和中间失败轮次均保留。

宿主是 Fedora 44、内核 7.2.11、rootless Podman 5.8.9，详细信息见 [host.json](host.json)。Debian 13/Fedora 44/Arch 是同一宿主上的三个客体，不能解释为三种宿主或桌面通过。共享项目继续使用真实数据根/bind；本轮只调整停止环境的只读管理投影。

## 实现变化

1. **先修普通文件对象与内容。** 节点使用 provider/mount/dev/ino 身份和 O_PATH anchor；同 provider 的普通文件硬链接共用节点，跨 provider 分开。新 lookup 重新辨认名称绑定；每次 OPEN 持有独立只读 FD，READ 用 pread，GETATTR(fh) 用同一 FD 的 fstat。replace/unlink 后旧句柄仍读原对象，新打开看新对象；不再逐块按路径重新打开。
2. **明确 live 契约。** entry/attr TTL 0，保留 cached I/O 和只读 mmap，按内核能力协商 AUTO_INVAL_DATA。普通文件权限位来自当前 fstat。停止客体不等于冻结宿主 bind/volume；同 inode 原地写仍可见，这不是内容快照。状态接口明确 watcher=false、snapshot=false、ACL unknown、xattr unsupported。
3. **再修目录拓扑与分页。** lookup/readdir 共用 MountGraph，合成必要祖先、统一覆盖类型，不从选定 provider 回退到被覆盖的 merged underlay。OPENDIR 持有有限生命周期的合并列表、稳定 cookie 及条目内部节点引用；READDIR 分页不重新扫描；源失效时新目录流明确失败，旧流维持自己的列表。
4. **回收与资源错误。** nlookup/open/internal 引用管理 FORGET、BATCH_FORGET、RELEASE/RELEASEDIR；活跃引用保留，短期路径 API 在成功和失败后回收父/子临时节点。目录总条目/总账面字节、节点和句柄预算明确拒绝超额请求，失败回滚锚点/FD；EMFILE/ENFILE/ENOMEM 不再被误报为后端身份 EIO。账面字节不是实际 RSS/slab/内核内存上限。
5. **保守处理别名歧义。** 相对 symlink 被硬链接到不同目录时，FUSE READLINK 只有 inode、没有调用者别名，当前返回 EOPNOTSUPP，避免静默使用首次发现路径。此项是明确拒绝策略，不是完整相对硬链接 symlink 支持。

环境 UUID/generation/CID 绑定、lease、storage broker、只读/noexec/nosuid/nodev、busy revoke 和保守恢复边界保持。实现细节见[设计说明](../../设计/投影对象与目录修复-2026-10-26.md)。

## 最终验证

| 验证 | 结果 | 原始证据 |
|---|---|---|
| 全量 unittest | 399 总计，395 通过，4 条件跳过 | [完整日志](unittest-verified.log) |
| 主控真实内核 FUSE | 22/22 | [结果](kernel-accepted/result.json) |
| Debian 13 完整 broker → FUSE | 17/17，互斥、busy 保护与清理通过 | [三客体结果](broker-accepted/result.json) |
| Fedora 44 完整 broker → FUSE | 17/17，互斥、busy 保护与清理通过 | 同上 |
| Arch 完整 broker → FUSE | 17/17，互斥、busy 保护与清理通过 | 同上 |
| 主控补充契约反例 | 7/7 | [结果](controller-contract-final.json) |
| 源码冻结与保全 | 通过 | [保全与源码哈希](preservation-verified.json)、[机器结论](acceptance-result.json) |

4 项 unittest 跳过分别要求显式启用 GUI、Podman 生命周期、包安装及资源集成模块。本轮另行运行了真实 FUSE/broker 测试，没有把这些 skip 算作通过，也没有补做 GUI 或包安装验收。

真实内核用例包括：32→128、128→32、同大小替换，旧 FD 已读/未读，unlink/recreate、硬链接与独立偏移、原地改写/增长/缩短、已触页/未触页 mmap、250 次打开读取与并发原子替换、缺失祖先及覆盖类型、跨 provider inode、dirent/stat inode 一致、目录分页删除/新增/seek、目录 rename 后旧 dirfd、只读拒绝与 chmod 刷新、失效 provider，以及相对 symlink 别名明确拒绝。

merged 与持久挂载两个后端分别执行 1,200 个唯一名称的 create/stat/unlink/再次 stat 和重复开关文件，再降低**真实 worker** RLIMIT_NOFILE 至耗尽。耗尽期间旧句柄仍可读，恢复限额/关闭句柄后新打开成功。计数如下（各轮新增一个仍存在的 pressure-file，其节点可仍由内核 lookup 引用持有）：

| 后端 | 重复开关/名称 churn 前后 worker FD | 耗尽前额外打开数 | 错误 |
|---|---|---|---|
| resources | 41 → 42 | 47 | 24（EMFILE） |
| provider-resources | 42 → 43 | 47 | 24（EMFILE） |

最终 file/dir handle 及目录条目/字节计数均回到 0；内核仍持有的 lookup 节点不会强制驱逐。这是有限样本和显式预算/耗尽验证，不是长时间内存上限证明。

旧版本从备份恢复到隔离源码后运行相同当前探针：**3/22 通过**，见[旧版结果](baseline-verified/result.json)。其中包含新增的 symlink 拒绝策略断言；不把所有不通过项都解释为相同的旧版本缺陷。原 32→128 截短、旧 FD 跳新对象及分页漏项的具体失败保留在各单项结果中。

## 失败历史与清理时序

首版真实挂载暴露 dirent 与 stat inode 不一致及资源耗尽 EIO；补充反例还暴露跨流预算、临时节点与跨 provider underlay 回退，均修复后另起新轮次。首版三客体各有 dirent/权限模式刷新失败，见 [broker-attempt1](broker-attempt1/result.json)。这些记录未改写。

中间 [broker-final](broker-final/result.json) 的所有文件语义用例通过，但 Debian 在关闭 busy FD 后立即撤销仍 EBUSY；该次保留 view/lease/storage，阻止移除。后续检查打开句柄为 0，正常重试撤销和精确 CID 清理成功，见[正常重试记录](broker-final/post-retry-cleanup.json)。原因未证明，不能把这次失败改写为通过或断言必然由外部扫描造成。

随后将真实挂载点放到私有临时目录，等待异步 RELEASE/RELEASEDIR 处理完毕，再正常撤销；[broker-final-v2](broker-final-v2/result.json) 与最终 [broker-accepted](broker-accepted/result.json) 两轮三客体均清理通过。本轮未使用 lazy unmount 或强行驱逐活引用。临时挂载位置调整是夹具隔离措施，不是对未知 EBUSY 根因的产品修复证明。

## 保全与开放关口

最终保全核验 **21,458 个受保护的先前文件**，无意外修改；其中含 **20,983 历史证据文件、23 主状态文件**。原有 **27 容器**的身份、状态及关键字段一致，新增测试容器已全部移除，FUSE 与 Podman storage 挂载均无残留。最终源码哈希与实际使用的 `broker-accepted/project/src/` 完全一致。

仍开放：后端监听/主动失效与丢事件策略、真正快照与跨卷版本协调、READDIRPLUS/成熟协议 worker、复杂 symlink/ACL/xattr/并发复制元数据契约、F 性能矩阵与 profiling、更长压力、真实 Dolphin/KWrite/IDE 工作流、跨宿主/桌面/故障。新版本性能尚未重测，旧 F 倍数不能当新版本性能。一次中间 EBUSY 的原因仍 UNKNOWN。普通项目热路径继续使用直接共享数据根。

复验应为探针的 `--out` 指定新目录，避免覆盖已有结果；`real_fuse_probe.py` 与 `real_broker_matrix.py` 均使用隔离数据，后者会创建并精确清理新的三套测试客体。主状态和历史证据不作为测试夹具。
