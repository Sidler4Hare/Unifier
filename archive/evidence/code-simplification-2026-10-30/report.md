# Unifier 第二轮代码精简（2026-10-30）

依据[代码质量审查报告](../code-quality-2026-10-30/report.md)，本轮集中减少受控清理和应用发现中的重复流程，拆开长函数。首轮原子写合并与恢复修复已在起始工作树中，本轮没有把它们重复计算为新增成果。

## 改动

| 位置 | 修改前 | 修改后 |
| --- | --- | --- |
| `file_operations._cleanup_run` | 277 行，混合恢复、捕获、校验、删除和回收 | 33 行协调入口；分开恢复删除后状态、隔离源文件、校验并删除 |
| `service.discover_app_entries` | 73 行，混合解析、原生读取和候选收集 | 42 行；纯解析移到 `app_entries.py`，候选按完整 argv 去重并保留首个来源 |
| `service.py` | 2,093 行 | 2,009 行 |
| `file_operations.py` | 2,706 行 | 2,678 行 |
| 全部产品 Python 源码 | 22,622 行 | 22,609 行，净减少 13 行，包含新增模块 |

- `_CleanupResources` 显式保存本次尝试拥有的 FD 和租约，外层仍在错误记录之后先释放目标租约、再释放源租约、最后按原顺序关闭 FD。源捕获后复用原打开描述，不重新打开受租约保护的文件。
- 合并三处租约获取错误处理、两处删除边界身份重验证、隔离状态写入、删除后收尾和九处重复 FD 关闭循环。清除全仓无引用的私有 `_LeaseLost` 类和无效局部变量。
- `app_entries.py` 承担 desktop Exec、候选路径分类和首个 Exec 行解析；包文件清单解析归入 `packages.py`。历史 `multilinux.service.parse_desktop_exec` 导入继续可用。
- 三类应用发现查询共用 `_capture_discovery`，保留原 argv、超时、`native=False` 和启停所有权处理；包事务和本地包元数据读取没有接入这个 helper。
- 修正模块顶部“源永不删除”的旧阶段说明，明确默认保留源与显式受控清理的区别。

持久日志状态、故障注入点、UUID/代际/CID 校验、路径锚定、锁序和 registry schema 均保持原协议。源码只净减少 13 行；主要收益是重复分支减少、资源所有权集中和主流程变短，不把移动到新模块的行数算成删除。

## 验证

- 修改前全量：531 项，527 通过、4 条件跳过，见 [unittest-baseline.log](unittest-baseline.log)。
- 最终全量：538 项，534 通过、4 条件跳过，见 [unittest-final.log](unittest-final.log)。
- 新增原生文件系统检查覆盖 9 个清理日志阶段的子进程退出：`SOURCE_CLEANUP_INTENT`、`SOURCE_QUARANTINE_INTENT`、`SOURCE_QUARANTINE_READY`、`SOURCE_CAPTURE_INTENT`、`SOURCE_CAPTURED`、`SOURCE_ISOLATED`、`SOURCE_DELETION_INTENT`、`SOURCE_REMOVED`、`DONE`。逐阶段确认恢复完成、目标哈希、源清理、隔离目录清理和二次恢复幂等。
- 在源、已捕获源、已发布目标三处注入租约获取失败，确认原字节保留，FD 数与信号屏蔽状态恢复。包含新增场景的 13 项受控移动测试也已针对备份中的原始源码执行通过，见 [cleanup-contract-before.log](cleanup-contract-before.log)，因此这些场景检验的是保留行为。
- 新增 5 项应用发现测试，包含 6 个查询失败/原运行状态组合、原生清单格式、字面参数、组件身份、候选顺序/去重、不可读取 desktop 和旧导入兼容。最终全量已覆盖这些测试。
- 全仓 `git diff --check`、公共 CLI help 和产品源码 Python 3.10 AST 语法检查通过。AST 检查不等于在 Python 3.10 解释器上完成运行验收。

4 项真实 Podman/GUI/资源/包集成测试仍按原条件跳过；本轮没有新的 Podman、真实 FUSE、GUI 或跨宿主验收。原生文件测试使用临时目录、真实 Linux FD/租约及子进程退出，不代表断电耐久性验收。

## 保留的缺口

投影外部 rename 的两个路径上下文反例仍然存在；本轮未改投影实现。重新执行原探针退出 1，两个用例均不符合预期，见 [projection-rename-open.json](projection-rename-open.json)。该结果明确是未解决缺陷，不计入通过数量。该问题需要独立处理对象身份、目录项上下文和旧 FD 生命周期。

没有扩大本轮范围去迁移所有状态字符串、重写 CLI、压缩历史 README/HANDOFF 或合并不同耐久性策略。

## 交付与恢复

- 起始分支：`codex/unifier-v2-control`；基线是当时完整工作树，含已有未提交/未跟踪文件。
- 修改前备份：`/home/omg/文档/Unifier-backups/20261030-110709-before-code-simplification`。129 个源码/测试/工具/配置/入口及文档文件逐项复制并校验，另存 HEAD、dirty diff 和 Git 状态。这是代码快照，不是用户数据全备份。
- 本轮只修改 4 个已有文件，新增 2 个代码/测试文件；其余 125 个基线文件哈希保持一致。原 README/HANDOFF、CLI 改动、旧证据及其他 dirty 工作保留。
- [changes.patch](changes.patch) 是相对于该备份的独立补丁，包含新增文件；[source-fingerprints.json](source-fingerprints.json) 保存最终修改文件指纹；[result.json](result.json) 保存统计。
- 未执行日常容器启停、重建、下载或部署同步，未暂存、提交或推送。工作区入口后续会加载本轮代码；独立日常部署副本未同步。
