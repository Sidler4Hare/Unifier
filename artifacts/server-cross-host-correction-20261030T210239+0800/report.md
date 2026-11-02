# 同派系约束修正与六项实测

2026-10-30，Asia/Shanghai。本报告更正上一轮 `server-validation-20261030T204043+0800` 的验收口径：**Ubuntu → Debian、Debian → Debian、Arch → Arch 三项不符合用户确认的设计，全部撤回“通过”判定。** 旧原始回执保留，不改写历史执行事实。

问题确实落在设计与实现：旧文档、`compatibility.py` 和测试契约都把 `same-family` / `same-distribution` 当作允许组合。首轮驱动又仅按已缓存镜像选择目标，漏掉四个跨派系单元。此次同时修正规则和测试矩阵。

## 当前规则与改动

同派系一律禁止创建、启动、执行及容器包事务，包含 Ubuntu → Debian。统一策略版本改为 2，CLI 在镜像检查或拉取前拒绝显式同派系选择，交互菜单排除该类目标。`init/configure/adopt/start/install/run` 共用该规则；已有容器处于运行状态也不能让 `start` 绕过拒绝。

默认镜像拉取、基础 smoke 与无桌面探针只选实际宿主准入的跨派系目标。查看、停止、精确删除及恢复数据的入口保留；不自动删除旧同派系容器，也不把拒绝静默转换成宿主命令。状态输出显示当前兼容判定。

设计说明、CLI 说明、Ubuntu 验收步骤、扩大测试计划、README 和 HANDOFF 已同步。真实客体仍只有 Debian 13、Fedora 44、Arch；RHEL、SUSE 等是宿主白名单分组，不是已经实现的新增客体。

修改前源码在 `before-policy-change/`，哈希清单在 `before-policy-change.sha256.json`。`same-family-policy.patch` 只描述本轮相对于修改前工作树的差异。原有未提交工作和暂存内容保留，Git 索引哈希未变，没有提交或推送。

## 修正后的实机矩阵

| 宿主 | Debian 13 客体 | Fedora 44 客体 | Arch 客体 |
|---|---|---|---|
| Ubuntu 实体机 | 同派系拒绝，通过负例 | 基础实测通过 | 基础实测通过 |
| Debian 虚拟机 | 同派系拒绝，通过负例 | 基础实测通过 | 基础实测通过 |
| Arch 虚拟机 | 基础实测通过 | 基础实测通过 | 同派系拒绝，通过负例 |

六个正向单元均使用修正后的源码，验证真实客体身份、共享内核、原生包查询、命令退出码、停止与同 CID 重开，以及 CLI 初始化、专用目录双向读写、退出码 17 传递。三台机器都实测同派系初始化、创建、拉取和显式 headless 目标被拒绝，没有创建相应环境记录或同派系容器。

Debian 上原有的同派系容器 `ml-05abfab9-debian13` 保留。使用新源码、原安装根执行 `env start` 和 `env run` 均明确拒绝，原记录、CID、状态和时间戳未改。三台机器原有容器前后对照均一致；本轮临时容器已正常停止并精确删除，任务挂载残留为 0。

本轮进入服务器仍使用 `agent`，虚拟机复用服务器已有的 `dev` SSH 配置。Podman 用户视图和可访问的 Docker 视图已补盘点，见 `full-runtime-inventory.json`。没有将其他运行时中的普通镜像当作 Unifier 已测容器。

## 验证证据

- 本机完整离线检查：558 项，4 条件跳过，无失败。见 `offline-regression-final.log`。三个基础假运行时改用显式模拟 RHEL 宿主，使不相关单元测试的三客体都保持跨派系；这不算 RHEL 实机验证。
- 三台真实宿主分别运行 34 项策略及副作用边界测试，均通过；逐机日志在各 `results*/policy-unit-tests.stderr`。
- 源码包 SHA-256：`f4f84fc72261385097b0a12712eb36cd72d622c085484bc89515244252729552`。源码在 `unifier-policy-v2.tar.gz`，200 个源文件的逐项哈希在 `source-manifest.json`，各远端执行前已校验。
- 本轮 driver 初稿把 `env create` 的参数写错，返回了参数错误；这个失败不计作策略拒绝通过。Ubuntu/Arch 用正确的 `--target/--id` 补跑，原失败和 `same-family-create-recheck.json` 均保留。Debian 初稿因 driver 自身导入路径遗漏而中断，在 `results-retry` 用修正 driver 完成全部流程，未覆盖原件。最终组合判定与回执引用在 `summary.json`。
- Fedora/Arch 锁定镜像只下载到一份带原 manifest 的缓存，再经局域网分发到 Debian；源 manifest 哈希与锁文件相符，导入使用 `--preserve-digests`。没有用转换后的镜像冒充原 digest。
- 文档中的 `python3 tools/lab.py doctor` 入口也已修复，三宿主直接调用通过。

## 范围与未完成部分

这次通过的是同派系约束、六项基础容器生命周期和无桌面 CLI。上一轮发现的 Python 3.12 文件租约兼容、停止状态 `/etc/mtab` 投影列举、快速改写时间戳碰撞及虚拟 GPU 问题未在此次修复。旧版固定三客体、GUI、安装事务及资源专项集成仍需按新准入矩阵安排，四个默认条件集成跳过不算工作流通过。

本轮没有实现宿主原生应用统一路由，也没有完成 GUI 应用安装/保存重开、完整 Agent 扩测或性能验收。新代码已在独立远程测试副本执行；本机工作树已修改，旧日常部署目录没有被替换。整体产品仍不是全部验收通过。

原始失败、补跑证据、源码和修复前备份都保留；交付文件用 `ARTIFACT-SHA256SUMS` 核验。
