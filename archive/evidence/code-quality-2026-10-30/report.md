# Unifier 代码质量审查与首轮精简（2026-10-30）

结论：项目已有较完整的身份约束、生命周期锁、事务日志、保守恢复和真实验收积累，但异常后的重试路径仍有遗漏；一些底层机制重复实现，部分大模块混合了多种职责。可以逐步精简，不适合为了减少行数重写整个控制层。本轮修复三处复现问题，合并六处严格原子写实现和两条命令的承接处理，产品 Python 源码从 22,712 行降到 22,622 行，净减少 90 行（约 0.4%）。新增故障回归测试的价值高于净行数。

审查基线是分支 `codex/unifier-v2-control`、HEAD `5e13d0d` 加上当时未提交的 CLI/兼容性改动；不是仅审查 HEAD。当前最新工作树的 523 项基线为 519 通过、4 条件跳过。保留已有改动，不提交、不推送。

## 发现与处理

### 1. 承接的旧环境在失败重试后被改成新环境（高优先级，已修复）

位置：`src/multilinux/shortcuts.py` 的 `_prepare_target`、`cmd_init`、`cmd_configure`。

触发：先 adopt 一个旧环境；容器暂时不可见时 init/configure 将状态改为 failed；原容器恢复后再次执行。旧实现用瞬时 `state == adopted` 决定是否走承接路径，而 `_prepare_target` 会把 `source` 改为 new，随后在新的安装根创建另一套空环境。旧容器没有被删除，但用户入口已经脱离原数据所在环境。

修复：按持久的 `source == adopted` 识别承接关系，统一在 `_prepare_target` 里验证源根、UUID、代际和 CID。失败保留身份，重试成功恢复 adopted 并清除旧错误。init/configure 不再各自实现一套重复而不一致的处理。

验证：两条命令均经历“承接→失联→失败→恢复→重试”，检查不新增运行时事件、不更改旧记录字节、不改变身份、不产生新环境记录。

### 2. 包事务预检失败会停止原本运行的容器（高优先级，已修复）

位置：`src/multilinux/service.py:1338`，`_package_transaction_locked`。

触发：容器已由用户启动；宿主快照或其他环境指纹检查抛错。旧实现预设 `was_running=False`，尚未检查容器状态，就在 finally 中调用 stop。此时没有执行任何原生包命令，却改变了用户运行状态。

修复：初始状态为未知，只有 `_ensure_running` 成功返回“本事务启动了容器”时才进行对应停机。预检异常不获得停机所有权。启动中途失败且未成功返回的情形也保守保留，不猜测所有权。

验证：分别注入两处预检失败，断言事务失败、native 为空、容器仍运行、运行时事件清单不变。

### 3. 租约释放失败后丢失恢复凭据（高优先级，已修复）

位置：`src/multilinux/project_sessions.py:972`，`recover`。

触发：确认 guest 已退出后，释放租约因日志写失败等原因返回 `release-error`。旧 recover 只拦截一个该 helper 不再返回的 `lease-without...` 字符串，因此仍清空 `lease_token` 并返回 recovered，而共享租约里可能还保留该 token。

修复：只有 None、released、already-absent 三种明确结果可以完成恢复；其他结果保持 unresolved 和原 token，返回 `unresolved-lease-release`。

验证：注入真实 release 调用异常，检查 token 仍可定位；解除故障后重试成功，第三次恢复幂等。

### 4. 投影缓存把对象身份与路径上下文绑定在一起（中优先级，未修复）

位置：`src/multilinux/projection.py:483` 的 `_allocate_node` 复用 inode 对象时保留首次 path；`projection.py:576` 的 `_lookup`、`:846` 的 `_snapshot` 以及 `fuse_view.py:309` 的 READLINK 又依赖这个旧 path。

已独立复现两例：

- 先 lookup `/a/link -> target`，外部把链接移动为 `/b/link`，再次 lookup 后仍复用旧 Node。READLINK 成功返回投影中的 `/a/target`，正确上下文应是 `/b/target`。
- 配置挂载 `/a/nested`，lookup `/a` 后外部把底层目录改名为 `/b`。再次读取 `/b` 时错误注入 nested，且能读到原挂载数据。

证据为 [projection-rename-open.json](projection-rename-open.json)。这是临时真实目录配合后端/协议捕获的反例，没有真实 FUSE 挂载或容器端验收；不能扩大解释为已验证的越界访问或真实客户端全部行为。

复现：`python3 evidence/code-quality-2026-10-30/projection_rename_probe.py`。当前预期退出 1，表示产品语义不符合预期，不能将这个结果计入通过数量。

后续需分离“对象/打开句柄身份”和“目录项/路径上下文”，或在无法确认上下文时明确拒绝。简单覆盖 `node.path` 会影响仍持有旧引用的调用；删除旧对象又可能破坏已打开 FD，所以本轮未做局部猜测修补。应覆盖重命名、硬链接别名、旧 FD、相对 symlink、嵌套挂载、FORGET 和回收组合后再真实挂载验证。

## 已完成的精简

新增 `src/multilinux/atomic_io.py`，由以下六处复用：EnvironmentStore、JsonRecordCollection、DataRootRegistry、文件操作 journal、桌面文件发布、view storage journal。

保留各调用点的 JSON 格式、目录创建权限、临时文件前缀、锁和调用顺序。公共函数仅负责同目录临时文件→文件 fsync→replace→目录 fsync，以及异常时清理临时文件。旧 `json_records.fsync_directory` 和 `view_journal.fsync_directory` 导入路径继续可用。

新测试覆盖：替换后旧打开 FD 仍读旧 inode、新文件权限 0600、文件 fsync 的 ENOSPC、replace 拒绝、目录 fsync 失败、KeyboardInterrupt 和临时文件/目录 FD 回收。目录 fsync 失败发生在发布以后，会抛错但新字节可能已可见；不能声称这种失败会回滚旧文件。

未将 `install_state`、语言偏好和 export 强行接入：它们现有的耐久性语义不同。前者吞掉目录 fsync 错误，后两者没有目录 fsync。建议后续明确哪些状态必须耐久、哪些只是偏好/导出，再统一策略；本轮没有模拟断电证明数据丢失。

## 仍需改进的维护结构

| 优先级 | 现状 | 建议与验证边界 |
| --- | --- | --- |
| 先处理 | 上述动态 rename 的路径上下文缺口 | 先完成针对性语义设计和反例，再修改投影核心；不是格式整理 |
| 下一轮 | file_operations 仍有 2,706 行；`_cleanup_run` 原基线为 277 行，串联多阶段恢复 | 按 journal 验证、目标发布、源隔离/清理拆分；保持持久状态名、锁序和失败窗口，逐窗口复跑，不抽象成通用事务框架 |
| 下一轮 | service 2,093 行，同时管理容器、包、应用发现和桌面导出 | 先抽原生包元数据/desktop entry 解析等无副作用部分，再考虑事务编排；统一接口不应削弱归属校验 |
| 后续 | shortcuts 1,957 行，命令定义、选择交互和安装恢复混合 | 可按配置/安装运行/目录查看组织命令；公共状态验证集中，避免再复制生命周期逻辑 |
| 后续 | 异常结果大量使用字符串和 dict，例如 recover 原来的失效字符串判断 | 对状态结果使用集中常量或小型类型，并验证未知分支；不要把所有现有记录 schema 一次迁移 |
| 后续 | README/HANDOFF 同时保存当前入口和多轮历史结论；模块注释也有过时阶段说明 | README 留快速开始、当前能力与限制、验证入口；历史保留在证据索引。比如 file_operations 顶部仍说源永不删除，已不能概括显式 controlled cleanup |
| 后续 | 根目录没有 pyproject/自动 CI 配置，验证主要靠手动 unittest 与分散证据 | 首先固定离线检查入口和最低 Python 版本矩阵；真实 Podman/FUSE/GUI 测试仍单独显式启用，不能因 CI 无宿主条件而冒充通过 |

不建议精简掉：环境 UUID/代际/CID 重验证、锁序、失败 journal、旧证据和 legacy worker。它们承担恢复与审计职责。`cli.build_parser` 虽有 446 行，但主要是声明式参数，并不是本轮最高风险点；不能只按行数排重构优先级。

## 验证与交付范围

- 原始三处缺陷：新增 3 个测试、5 个子场景在修改前全部按预期失败，见 [regressions-before.log](regressions-before.log)。
- 最终全量：见 [unittest-final.log](unittest-final.log) 和 [result.json](result.json)。4 项真实 Podman/GUI/资源/包集成按原开关跳过；测试数量不代表本轮完成 GUI/FUSE 或跨宿主验收。
- 投影 rename 两项反例仍开放，与回归通过分别报告。外部通知、严格快照、完整元数据、IDE/GUI 和跨宿主能力仍应依当前证据逐项验证。
- 只改工作树源码与新增测试/本轮证据，没有执行日常容器启停、重建、部署同步或下载。用户命令入口原已指向工作区，因此后续调用该入口会加载新代码；独立日常部署副本未同步。
- 修改前备份：`/home/omg/文档/Unifier-backups/20261030-000715-before-code-quality`，126 个源码/测试/工具/配置/入口及 README/HANDOFF 文件逐项校验。另存原 dirty diff 和 HEAD。此备份是代码快照，不是容器/用户数据全备份。
- [changes.patch](changes.patch) 是相对于本轮修改前工作树的独立补丁，含新增代码和测试，避免把原有未提交工作误算为本轮改动。没有提交或推送。
