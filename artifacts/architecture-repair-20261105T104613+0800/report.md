# 架构修复第一批阶段回执

日期：2026-11-05（Asia/Shanghai）。总体 **PARTIAL**。S0 已完成；S1/F02 已实现并通过限定范围 T1 验收；S2/F01 完成统一观测契约及停止态 Debian 适配的离线验收，真实能力仍未放行；S3/F03、S4/F04、S5 真实专项未完成。未提交、构建或部署。

## 实现和输入保全

输入 HEAD：`6f776a61dc599ad8f1c0a476681e892248e55a6b`，不能代表全部输入。原脏文件、索引哈希和源码/文档摘要见 [before.json](before.json)，逐文件恢复副本位于 `before/`。本轮独立补丁见 [this-run.patch](this-run.patch)，包含新文件；不把先前脏改动归为本轮实现。

- S1：管理根级跨进程 flock 覆盖包事务前置观察、原生步骤、库存提交、后置观察及最终记录持久化。install/update/remove/install-local/repair/recover、创建、删除和身份迁移协调；环境包锁保留。默认排队 30 秒，支持取消/繁忙/超时，锁文件和恢复事务保留。应用启停不回取协调锁。
- 隔离比较新增完整实例绑定与观测证据；外部库存、集合或代际变化仍拒绝，不吸收为新基线。
- S2：真实 PodmanRuntime 和 fake 使用 `InventoryObservation`。实现停止态 Debian 只读原生 DB 锁、限定文件/字节/时间、路径及实例复核；有更新日志、忙/损坏数据库、链接逃逸、代际变化或释放不明时拒绝。原清单序列化和既有事务字段保留，新增证据字段。
- 存储 helper 的 mount 引用单独记账，只释放自己确认取得的一次引用；不确定日志持续阻断，没有自动删除或模糊 umount。真实 helper 尚未执行。
- Arch、RPM 和运行态同伴明确 unsupported；Debian 未证明等价的数据库状态也拒绝。具体边界见 [接口说明](../../docs/包清单只读观测.md)。

用户在本轮明确将宿主保留量 R 及对应低内存中止线从 2 GiB 改为 **1 GiB**。已同步 AGENTS、全项目规范、本机细则、共享专项及架构计划；其他硬预算、PSI、独立监督、30 秒准入和停止规则保持。旧历史证据及旧保护器的数值没有伪改为本轮结果。

## 修复前后与用例状态

原 CLI 命名空间反例先在当前工作树重放，确认失败于同伴清单 `isolation-preflight`，native 为空：[原始失败](baseline/command.log)。之后同一用例按期望正确行为重放通过：[修复后](s2-offline/command.log)。该回归使用 fake，不能代替真实数据库能力。

| 用例 | 本轮结论 | 证据/范围 |
|---|---|---|
| FIX02-01 | PASS T1 | 两个 fake 环境排队，第二个等到首轮最终记录持久化后执行，两者 completed |
| FIX02-02 | PASS T1 | 两个独立进程模拟写入均成功，enter/exit 不交叠；另测竞争超时 |
| FIX02-03 | PASS T1 | 原环境包锁保留；全部包入口及集合变更等待超时前无 start/native；取消不进入写阶段 |
| FIX02-04 | PASS T1 | 崩溃释放 OS 锁，原 pending 文件仍在；needs-recovery 不被队列准入绕过；相关恢复回归通过 |
| FIX02-05 | PASS T1 | 外部库存、添加/删除同伴和代际变化仍失败 |
| FIX02-06 | PASS T1 | 有界事件交叉 install/repair/recover 与 APP/项目 lease 释放；忙时 stop 立即拒绝，写入后可按原所有权收尾 |
| FIX01-01 | PASS T1 | 原 CLI 反例及无/一个/多个同伴的相关离线消费者通过 |
| FIX01-02 | PARTIAL | fixture 规范化、版本/架构/状态边界通过；尚未与目标 Debian 的原生 dpkg-query 实际对照 |
| FIX01-03 | PASS T1 / T2 NOT_RUN | 忙、损坏、限额、路径/实例替换、代际变化、存储释放失败和前置拒绝覆盖；真实故障边界未测 |
| FIX01-04 | PASS T1 | fake 与真实类采用同一结果类型；直接测 PodmanRuntime 方法、helper 命令和释放证据契约，底层仍为离线 runner |
| FIX01-05 | BLOCKED | 固定池两个实例不在同一管理根；未获得合规多环境 fixture，未满足 PRE03；不新建/迁移补齐 |
| F03 / F04 | NOT_RUN | 本轮未改 APP 实际执行所有权或 ELF 加载判定；原反例仍是未解决项 |

相关 T1 最终批 **136 项通过**；随后补充 APP/项目 lease 与三种包操作交叉锁用例 **1 项（3 个分支）通过**。计入前面受影响的包/环境模块并按测试 ID 去重，共 **166 项通过**，不是各批简单相加。完整 ID 及各轮回执见 [preservation.json](preservation.json)。没有运行全仓库套件、真实集成或 GUI。

补充交叉锁用例首次未进入待测边界：PackageBoxes 没有 `test -x` 的 fixture 响应。原错误保留在 [首次日志](lease-lock-boundary/command.log)；仅修正该新测试的微型可执行声明及 repair 输入，未改产品或放宽断言，一次针对性重放通过：[更正说明](lease-lock-boundary-corrected/basis.md)、[结果](lease-lock-boundary-corrected/command.log)。

## 模型、资源与无效样本

所有 T1/只读元数据作业均串行，实际 cgroup 核验 512 MiB/96 任务/1 CPU/0 swap、MemoryHigh 384 MiB、总期限 120 或 300 秒；独立保护器 64 MiB/16 任务、250 ms 采样，负载前连续 30 秒低压力准入。各目录 `receipt.json`、`samples.jsonl` 和 `worker-final-cgroup.json` 保留原始证据。

按 AGENTS 通过 dsh 最小 provider overlay 验证 `local-server / swift-1.5-iq3_s / max`，无远端回退。Qwen 任务在 full PSI avg10=3.1% 持续约 2 秒时被定向中止；本轮未得到模型代码产物，后续由主控直接实现，没有重试该模型任务。[中止回执](qwen-s1/receipt.json) 不能计为通过。日志显示客户端峰值约 287 MiB，未命中 512 MiB 硬内存上限，不能仅凭本次相关性认定宿主 PSI 根因。

全部阶段观测到的最低 MemAvailable 约 2.50 GiB；本轮实际执行阈值为用户指定的 1 GiB。有效离线批无 OOM/pids/max 事件；CPU 节流有记录，本轮不作性能结论。

## 原存储元数据与真实验收阻断

第一次只读 Podman 查询继承了 T1 的私有 HOME/XDG，落在空的临时存储中，未找到固定 CID；该样本为 **INVALID_CONTEXT**，不是实际容器消失或产品缺陷。保留 [错误日志](storage-interface/command.log)，没有将空清单计为保全证据，也没有启动、创建或挂载对象。

唯一纠正后，用原 HOME/XDG 存储定位重新准入，仅执行 version/ps/精确 CID inspect：[依据](storage-interface-original/basis.md)、[原始摘要](storage-interface-original/interface.json)。实际 Podman 为 5.8.9；容器总数 **10 → 10**、完整 ID 集合不变；固定 Debian 和 Arch 均 exited，StartedAt/FinishedAt 未发生本轮启停。未执行库存 helper、podman mount、guest exec 或包事务。

Debian/Arch 分别仍属于原有不同管理根，不能作为 FIX01-05 的合规同根多环境 fixture。当前 Arch 原配额及 PRE03 缺口没有自动修改；真实读取、加载、APP/GUI 工作流均没有以离线绿灯代替。其他容器没有逐个采集完整前后状态或连续事件；本轮未向其发出 create/start/restart/exec/stop/remove，不能冒称完成全体容器生命周期验收。

## 收尾、安装版与剩余工作

[保全核验](preservation.json)：限定源码/文档无越界变化，原未跟踪文件无缺失，Git 索引不变、boot ID 不变。所有本轮受管 cgroup 已移除，全部本轮单元无 MainPID/活动 cgroup：[精确单元回执](units-after.json)。保留失败单元状态及全部中止锁，不做全局清理。KWin 起始 PID 未单独采集，不能声称验证其 PID 不变；本轮没有操作桌面、键鼠或会话。

安装入口仍指向 `/home/omg/.local/share/unifier-code/releases/0.0.2-b656bf73abfe`，其 service/runtime 与当前工作树不一致。**工作树修复不等于已部署**。本轮没有原生包数据库、账号、用户文档或日常容器操作；没有提交、构建、发布、推送或删除历史对象。临时根和错误上下文缓存保留供复核，没有将它们用作新的容器 fixture。

下一步需要：Debian 原生清单等价性、存储引用实际获取/释放及 PRE03 保护适配；FIX01-05 的合规同根 fixture 仍是阻断项。S3/F03 客体执行所有权与旧 lease 恢复、S4/F04 静态候选和真实加载分层继续按原计划实施。该阶段回执不宣布整体架构修复完成。
