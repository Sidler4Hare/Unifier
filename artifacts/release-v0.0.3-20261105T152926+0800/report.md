# Unifier v0.0.3 组装与依赖自动化迭代

日期：2026-11-05，Asia/Shanghai。状态：**源码包构建与限定离线/私有安装验收 PASS；真实容器、GUI 和日常部署未验收。** 基准 HEAD 为 `fbfbece91e859289de1cbdad5f467cc74d5301f1`；本版新增改动仍在工作树，精确交付以逐文件清单为准，没有将旧 HEAD 冒充本版源码。

## 交付物

- [unifier-0.0.3.tar.gz](../unifier-0.0.3.tar.gz)：792,131 字节，255 个源码/文档/链接条目，另含包内 BUNDLE-MANIFEST.json。
- [SHA-256](../unifier-0.0.3.tar.gz.sha256)：`9aa7885ad6e81d07ebc19d34ae82583e6cb940b4ecda0c092c98f53d24597a96`。
- [逐文件清单](../unifier-0.0.3.tar.gz.manifest.json)、[独立解包完整日志](../unifier-0.0.3.tar.gz.validation.log)。
- [本轮差异](changes.patch)、[改前指纹](before.json)及 before/ 恢复副本、[静态与保全核验](validation.json)、[机读结果](result.json)。
- [版本说明](../../docs/v0.0.3.md)与[后续自动化路线](../../docs/依赖自动化迭代.md)。旧发布包不覆盖；源码包不包含容器、用户数据或私有模型配置。AGENTS.md 现在随包交付。

## 实现

汇总已有请求桥、执行控制、包事务协调、停止态 Debian 清单、APP subreaper 租约及依赖证据分层。新增 dependency_diagnostics.py、`repair-deps --from-log` 与 `--diagnose-only`：明确 loader/dlopen 缺库错误 → 严格 SONAME 提取 → 原生仓库唯一提供者 → 正式包事务安装 → 复查。日志由调用方明确选择，不自动重跑应用。

诊断阶段纯本地，不构造 EnvironmentService 或访问容器。只保存类别、库名、输入 SHA-256/大小；新增诊断记录不保存原文/路径，原生命令输出沿用既有日志规则。输入上限 1 MiB/32 库，拒绝异常文件、读取中变化、非法文本和控制码。ABI/符号、权限、命令、未定位插件分类，混合故障在 runtime 访问前拒绝，不猜包、不改 wrapper、不取消签名或隔离。候选可进入查库阶段不等于提供者存在；包事务 completed 也不等于加载或工作流通过。

## 验证

- 独立解包副本全量离线：**802 项，797 通过、5 条件跳过、0 失败**；216 个 Python 文件语法、42 个旧模块别名、五个 CLI 入口均通过。15 项新增测试覆盖解析边界、实际事务接入、持久化先于 native、无效/歧义提供者及诊断无 runtime 副作用。原有真实集成用例没有启用。
- 初版定向 58 项通过；随后将本地候选字段明确为 library_repair_candidate/provider_resolution=not-run 并增加混合故障 CLI 用例，最终版本全部由上述 802 项覆盖，不简单相加通过数。
- 初次整树基线 787 项有 1 个 Unix socket 路径过长错误，记 INVALID_FIXTURE。原失败保留在 [baseline](baseline/command.log)。缩短本轮私有 TMPDIR，未改产品/断言；[针对性重放](targeted/command.log)及最终完整解包回归均通过。
- [实际发布包验收](artifact-smoke-result.json)：私有目录安装原 v0.0.2 → 升级真实 v0.0.3 → 版本/日志诊断 → 通过 rollback 链接回到 v0.0.2；私有数据标记保持。清除继承的 PYTHONPATH，确认从独立包加载，未借用工作树模块。此项是私有安装入口验收，不是日常部署。
- [静态核验](validation.json)：当前 255 个包内条目与工作树一致；24 份入口/现行/设计文档的 136 个本地链接均存在。Git 现行差异空白检查通过。

## 模型、预算与收尾

仅一次 dsh 本地路由任务：local-server / swift-1.5-iq3_s / max，最小 overlay 脱敏 dump 通过，未回退远端付费模型。配置服务地址为数值地址、非回环且不属于本机接口；未启动或管理服务端。客户端在 220 秒内没有交付允许文件，任务失败并按登记单元定向收尾；主控接手实现，无重复模型尝试。[路由回执](model-route.json)、[位置核对](model-route-location.json)、[模型任务说明](qwen-summary.md)。该尝试不计验收通过。

每阶段串行，512 MiB/96 tasks/1 CPU/0 swap；独立保护器64 MiB/16 tasks，250ms采样、30秒准入、1GiB宿主保留线和独立总期限。所有阶段无保护中止或最终OOM/pids硬事件，CPU节流按原回执保留，不报告性能收益。

| 阶段 | 命令退出码 | 最低宿主 MemAvailable | 采样负载峰值 | 峰值任务数 |
|---|---|---|---|---|
| baseline | 1 | 3.323 GiB | 384.0 MiB | 9 |
| qwen | 1 | 3.501 GiB | 317.9 MiB | 37 |
| targeted | 0 | 3.624 GiB | 43.6 MiB | 5 |
| build | 0 | 3.557 GiB | 345.5 MiB | 11 |
| artifact-smoke | 0 | 3.780 GiB | 41.2 MiB | 3 |

[全部单元回执](units-after.json)：十个精确登记的 worker/guardian 均 MainPID=0、无活动 ControlGroup；所有本轮 cgroup 已移除。保留原失败单元状态，不清全局服务/进程。未创建显示/转发监听、未启动/创建/挂载/exec 项目容器、未执行真实包事务；容器总数未现场重盘点，不能填报全体运行状态已验收。

原 v0.0.1/v0.0.2 的八个包/校验/清单/日志哈希全部不变；246 个未修改基线文件哈希不变，11 个既有文件只在明列范围修改，无基线文件缺失。Git 暂存内容仍与原 HEAD 一致，没有暂存、提交或推送；索引字节哈希发生变化，不宣称字节原样保全。boot ID 不变；未访问/操作桌面或采集 KWin 身份。日常入口现场仍指向 `0.0.2-b656bf73abfe/bin/unifier`，没有切换。没有读写业务文档/账号或原生数据库；未对这些数据做全量哈希验收。

私有 T1 源码、安装测试数据和日志保留在 /tmp/unifier-v003-20261105、/tmp/u3t-152926、/tmp/u3p-152926、/tmp/u3s-152926；用于失败复核和安装回退证据，不作为新真实容器管理根。模型私有配置为0700目录/0600文件，不放入发布包或公开回执。

## 未完成边界

PRE03 真实容器汇总预算/监督/非目标事件与保护注入仍 BLOCKED；同管理根多环境 fixture、Debian 原生清单等价性与引用释放、Arch/RPM/运行态适配、模糊存储引用对账、目标客体 subreaper/loader、通用单实例、私有 GUI 保存重开、会议媒体、原生 Agent 和长压没有由本轮离线结果补成通过。最新版本的 Python 3.11 与跨宿主运行也未在本轮重测。

本版减少了手工摘库名/查库/拼补库命令；自动采集应用失败、版本绑定的持久诊断、已验证的加载/插件适配和可恢复修复计划仍按依赖自动化路线继续。没有将整个“依赖地狱”宣布解决。到此结束本次源码版本交付；日常安装、Git提交/标签、发布与推送分别待对应指令。
