# 只读投影 FD 修复与本机验收（2026-10-29）

结论：**目录浏览的逐条目 FD 放大已修复，worker FD 准入已实现并同步两个日常部署；大规模 stat/索引仍有受控的 lookup 容量边界。** 本轮没有把资源保护通过解释为无限容量、通用 POSIX、性能或跨宿主验收。

## 改动

依据[分析报告](../projection-fd-analysis-2026-10-29/report.md)实施：

- 目录清单只保留名字、类型、对外 inode 和稳定分页信息，逐条扫描后收集临时 child anchor；目录自身 FD 保留，原 lookup/open/internal Node 继续按对象引用存活。
- inode 碰撞表独立引用计数，同时覆盖活 Node 和轻量目录记录。硬链接、多目录流、回收后再 lookup、清单内/跨清单碰撞均保持检查，失败显式 EOVERFLOW 并回滚。
- FD 准入读取实际 worker soft/hard NOFILE 和整个进程使用量，包含 provider、anchor、handle、FUSE、控制等。每操作预留 1/4 个临时槽位，再留 16 个控制和启退余量；不提高全局或日常默认上限。路径 read 复用受管 handle，provider 路径 walk 临时 FD 不随深度增长。
- 增加 FD/引用/预算状态观测；测量失败返回 null 和 errno，保留真实 OS 资源错误。控制 accept 碰到瞬时资源错误会等待重试，不永久退出。
- 保持原子替换/unlink 后旧 FD、GETATTR(fh)、mmap、provider 边界、只读、稳定 cookie 和目录内存账面预算。路径兼容 readdir 也执行目录预算。

详见[设计说明](../../设计/投影FD预算修复-2026-10-29.md)。实现为 `src/multilinux/projection.py`、`view_backend.py`、`fuse_view.py`；旧 legacy 实现与历史证据未改。

## 先行备份与复原

修改前外部备份：`/home/omg/文档/Unifier-backups/20261029-141151-before-projection-fd/`，完整工作区包含 Git、未跟踪分析报告及重要 ignored 文件，共 36,060 条目复制/核验。5 个历史 Unix socket 只保留类型/路径元数据，不是活进程检查点。原版本可从 `workspace/src`、`workspace/tests` 及 Git 独立恢复；不需要清理日常容器或历史证据。

两个日常部署在原生命周期/租约锁内备份、核验后，原子同步上述三个模块：`/home/omg/.local/state/unifier-deployments/projection-fd-20261029-142459/`。该目录保留完整部署、文档、菜单及私有恢复元数据。恢复代码可在停止/无租约/无活动视图时，将各 app 的 `deployment/` 原文件及 deployment-current.json 放回原路径；不重建环境或迁移数据。

## 单元与失败边界

最终[全量日志](unittest-final.log)：**456 tests，452 passed、4 conditional skips**。相对原基线新增 7 项回归，已有 4 项内部引用断言改为新的轻量清单契约。没有把失败删掉或改成 skip。

新增覆盖真实子进程 NOFILE=256 的 300/3000/10000 条目目录、lookup 压力与 FORGET 恢复；外部降低实际限额及其他进程内 FD；清单碰撞、双目录流与硬链接引用；部分失败回滚和测量 ENFILE/EMFILE；80 层 provider 相对路径 walk 最多两个临时 FD；控制 accept 资源错误后的重试恢复。相关投影测试共 54 项。

初次相关回归的 4 个失败均是旧断言要求“目录保留子 Node/等到 RELEASEDIR 才收集”；更新为新引用契约后通过。最终完整测试与最终源码一起核验。

## 真实 FUSE：低限额与旧契约

[真实低限额结果](real-fd-final/result.json)，测试 worker 从启动起 soft NOFILE=256，宿主进程限额未变：

| 场景 | 验收结果 |
|---|---|
| 300、3000、10000 个文件的纯 readdir | 都完整列出；打开目录 FD 增量分别 2、2、2；节点仅增加目录自身，不增加每个条目的 anchor |
| 同目录两个并行流及公开 ls -U | 名字完整、无重复；关闭后目录 identity refs 和句柄归零 |
| 大量 stat/lookup | 预算明确 EMFILE，采样 worker FD=238/256；控制查询约 1 ms，旧文件仍读原对象 |
| 公开 ls -l | 在 lookup 压力下明确失败，保留为容量边界，不算成功索引 |
| 大量实际 open | 达到预算后仍可读旧 FD；unlink 后旧 FD 和 mmap 保留原内容；关闭其他文件后新打开恢复 |
| 正常退役 | 三组均正常卸载、worker 退出，无强制或 lazy unmount |

同时旧[真实 FUSE 核心 22/22](core-final/result.json)、[语义边界 9/9](boundaries-final/result.json)通过，涵盖大小变化、恢复 mtime、替换/unlink、mmap、链接组件、分页、provider 失效、资源失败回滚。第一轮与最终低限额结果分别保留，不覆盖已有证据。

## 三客体完整产品链路

[最终 broker 结果](broker-final/result.json)：在当前 Fedora 宿主，Debian 13、Fedora 44、Arch 各 **27/27**。使用真实 EnvironmentService → ViewManager → storage broker → FUSE worker；原 26 项语义用例保留，增加运行中将各隔离 worker 降至 NOFILE=256 的三档大目录检查。测试后恢复其原限额并正常退役。

每套均验证活动视图阻止启动/删除、旧对象语义、busy revoke 拒绝和正常清理；本轮 3 个测试容器全部按精确 CID 删除，视图注册/租约归零，原两个日常容器集合/状态保全。首次 broker 与最终源码 broker 结果分别保存。三种客体不是三种宿主，跨宿主继续开放。

## 日常部署与真实文件管理器

[部署回执](deployment.json)及[部署保全](preservation-deployed.json)：FreeCAD、腾讯会议均同步最终模块哈希。各自使用原已安装源码、原停止容器和完整 broker/worker，实际 worker 降至 NOFILE=256，**3000 条目列出、内容读取、EROFS、活动视图阻止启动、正常撤销**通过：[FreeCAD](installed-freecad/result.json)、[腾讯会议](installed-wemeet/result.json)。

最终两套原 CID、启停时间及整个 State、全部主状态文件、配置/镜像锁、文档和菜单哈希保持。没有启动日常应用容器、升级供应商包或重建环境；本轮不重复宣称应用菜单/会议/摄像头接受。

Dolphin 在继承默认高限额 worker 上打开 1000 个文件的实际投影目录，完整枚举结束（dir_handles=0，1002 个节点含根与目录），窗口显示正确路径和文件网格，正常关窗后句柄归零并卸载。[最终 GUI 结果](gui-final-3/result.json)，[窗口截图](gui-final-3/dolphin.png)。这也直接确认文件管理器会做 lookup/stat，并非只有纯 READDIR。

GUI 失败/不足记录保留：第一次脚本错误等待常驻 Dolphin 退出而超时；下一次截图时仅完成部分枚举；再下一次虽枚举完成，但截屏期间焦点切到其他窗口，图像丢弃且结果标记未接受。最终轮等待完整枚举、确认截屏前后焦点并逐图复核后接受。没有把工具成功码当作视觉验证。

## 保全、交付和仍开放的边界

[工作区保全](preservation-workspace.json)核验 32,306 个既有普通文件（不含生成的 pycache 和 Codex 自动维护的临时 turn-diff refs）：除声明的代码/测试/说明变更，无原件删除或意外改写；之前分析与旧证据保持原样。最终只保留两个原日常容器，均停止；所有本轮测试 worker、broker、视图、租约和临时目录均正常清理。源码冻结与机器验收结论见 [source-frozen.json](source-frozen.json)、[acceptance-result.json](acceptance-result.json)，最终 58 项汇总核验通过。

- 目录记录不再长期固定每个历史对象，仍是 live 名称清单；其身份元数据与名字清单占 O(N) 内存，账面 bytes 不是 RSS。
- 大量 stat/索引仍需活 lookup 锚点，内核何时 FORGET 不由用户 close 文件决定。不能强关仍引用对象来制造容量恢复；本轮实现受控拒绝与真实释放后的恢复。
- 16 个控制余量经过本机低限额/控制请求验证，不保证任意外部线程、突然恶意缩限或全系统文件表耗尽下永远能接受连接；真实错误会保留，资源恢复后 accept 重试。
- 每次准入枚举 FD 有开销，采样峰值不是内核精确峰值。本轮没有新吞吐 A/B、长压或通用性能结论。
- 外部 watcher、严格快照/跨卷、ACL/xattr、完整 IDE/索引、其他宿主和桌面仍开放。正常项目继续 bind 热路径。
- 后续按用户要求整理提交并同步云端，见[发布说明](publication.md)。acceptance-result.json 的 uncommitted 字段保留验收当时状态。10 月 28 日 Ubuntu 便携包保持历史冻结版本，未静默替换；另机应取本轮源码。

协议/限制核对参考：[libfuse lookup/forget/readdir](https://libfuse.github.io/doxygen/structfuse__lowlevel__ops.html)、[Linux RLIMIT_NOFILE](https://man7.org/linux/man-pages/man2/getrlimit.2.html)。
