# 本轮提交范围与云端同步（2026-10-29）

本次按用户要求，将只读投影 FD 修复的代码、回归、设计、分析、成功/失败验收记录及部署/保全证据，整理在现有私有仓库 `Sidler4Hare/Unifier` 的 `codex/unifier-v2-control` 分支。不改写远端历史，不合并 main，不改另一个工作树。

## 提交前盘点

- 已修改的跟踪文件 7 个，未跟踪文件 80,662 个，共 80,669 个待处理文件。
- 当前 HEAD `bb38f7a` 与远端同名分支一致，没有未推送提交；此前 3 个 v2 提交已在云端分支，仅未合入 main。
- 旧 `fix/wemeet-camera-perf` 工作树没有未提交文件。
- 80,466 个未跟踪项来自六套 broker 生成的共享目录夹具，其中绝大多数为大目录测试的空文件；不属于 8 万个独立源码改动。

## 可恢复备份与生成数据

全部待处理文件加原 .gitignore，共 80,670 项已归档逐项验证；Git 全引用 bundle 已验证。外部恢复路径：

`/home/omg/文档/Unifier-backups/20261029-144634-before-fd-publication/`

本次只给上述 broker 夹具添加精确范围的 .gitignore 规则，原件继续留在本地，外部备份保留文件内容和符号链接。它们由 real_fd_probe.py / broker_fd_probe.py 调用既有边界用例生成，结果、脚本、源码副本、原始日志和截图正常提交；无需把 300/3000/10000 个占位文件逐个放进 Git。原有 .lab、缓存与本机部署数据保持原忽略策略。

数量与备份摘要见 [publication-inventory.json](publication-inventory.json)。发布过程回执位于本机 `/home/omg/文档/多兼容linux机/.lab/publications/20261029-144634/`，以实际提交和远端分支 SHA 核对结果为准。

## 验证沿用范围

提交前核对产品源码/测试与 source-frozen.json、冻结副本及部署记录一致，已有 456 项回归（452 通过、4 条件跳过）、三客体 81 项完整链路、真实低限额与 Dolphin 检查证据保持。本次整理只调整文档和生成数据忽略规则，没有改产品实现，不重复启动日常容器。原始 audit.py/acceptance-result.json 记录的是提交前验收点；提交产生的 Git 元数据变化与本次 .gitignore 修改属于后续发布操作。

大量 stat/索引容量、watcher、快照/多卷、性能长压、通用 IDE 与跨宿主边界继续保留，详见[修复报告](report.md)。此前 Ubuntu 便携包仍为 10 月 28 日冻结版本。
