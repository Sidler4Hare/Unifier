# 共享专项续测回执

2026-11-09，Asia/Shanghai。总体 **PARTIAL**。本轮补齐固定 Debian 的 **SH01–SH08**；不能将整个 32 条专项标为完成。SH09、DEP01–06、DEP08 的真实通过及 DEP09/10 的离线通过沿用历史回执，没有在本轮重跑。最新台账共 19 PASS、7 PARTIAL、1 INVALID、5 BLOCKED。

## 已完成的共享运行时

使用工作树 `c947077f127cf142c78796bf64d6d1542c1ab091` 加已有脏修改，通过正式 `app` 路径及固定实例安全适配层执行。原管理根 `.lab/guarded-expansion-20261103/environment`，UUID `ae147c3e-3e65-41ac-ac8d-9257c9202696`，generation 1，完整 CID：

`e58754f0645ae1ff2924cb09a7264cc821e44893e7589a7fd0ebca09ce5f5e45`

| 用例 | 本轮实际证据 |
|---|---|
| SH01/02 | 三个轻量应用同一实例；同一冷组原生 start 事件恰好 1 次，热开不额外启动；三个调用的 argv/cwd/env/输出独立 |
| SH03 | A 正常结束后 B/C 的 PID、starttime、租约及持续增长的心跳保持，容器继续运行 |
| SH04 | 最后真实受管应用结束后自动停止，租约和 bridge 清空 |
| SH05 | 相同可执行程序的不同调用上下文、标记和输出不串扰 |
| SH06 | 仅终止本轮登记并核验的宿主短命启动器，真实客体执行和租约继续，后续按协议正常结束 |
| SH07 | 显式手动启动后运行 A/B，最后应用结束不擅自停止容器；本轮再明确停止以恢复原状态 |
| SH08 | 真实无效 APP 返回 2 且不影响存活应用；身份替换、未知执行和恢复拒绝边界由离线合同补充，不在实机伪造外来执行 |

原始判据、PID/start、租约及事件计数见 [shared-cli/result.json](shared-cli/result.json)，实际汇总 cgroup 覆盖见 [live-binding.json](shared-cli/live-binding.json)。本组命令原生退出 0，无保护原因，峰值 326.43 MiB、55 任务；宿主最低可用约 3.0 GiB。保护器同时覆盖控制器、init、conmon、网络和事件辅助进程。没有新增容器或进行包安装。

## SH10：无效样本与恢复

固定总预算 1 GiB/128 MiB swap/256 任务/1 CPU，原客体 768 MiB/192 任务，控制器使用已授权的 384 MiB 档位；独立保护器另有 64 MiB。宿主保留 1 GiB、连续 30 秒准入、250 ms 采样、PSI 和 90% 停止线均维持。GUI 使用客体私有 Xvfb、认证、HOME/XDG/TMP、软件渲染和合成文档副本，不连接宿主活动显示、键鼠、账号或音视频设备。

1. [首次 writer_cli](writer_cli/receipt.json)：`ABORTED_MEMORY`，峰值 978.18 MiB、86 任务。末样本 guest anon 约 119 MiB、file 约 574 MiB，控制器 anon 约 223 MiB。显示/Writer APP 请求被中止锁拒绝，不能归结为 Writer 内存泄漏。原始中止锁和日志保留。
2. 根据测量增加**可选、更紧**的当前客体 `memory.high=512 MiB`，没有放宽硬预算或阈值；先通过 35 项受影响保护合同，再进行唯一有依据的重试。新私有显示、目录和文档使用 `1110` 后缀，不覆盖旧样本。
3. [重试 writer_cli_r1](writer_cli_r1/receipt.json)：到达 [隔离 WPS 许可窗口](writer_cli_r1/initial.png)，随后 `ABORTED_PSI`。峰值 798.46 MiB、199 任务，最低宿主可用约 2.83 GiB；末样本宿主 full avg10 为 3.07%，超过 3% 并持续到停止条件。没有 OOM。较低内存不代表压力足够低，回收线不能保证 GUI 完成。
4. 两次均精确停止原 CID 及控制器。随后只允许 inspect/info 的恢复适配器调用正式 `app recover debian13`，分别释放三条已证实结束的租约，bridge 清空；见 [首次恢复](writer_recovery/result.json)、[重试恢复](writer_recovery_r1/result.json)。没有删租约文件、中止锁或更换普通 runtime。

**SH10 仍为 INVALID，本轮 GUI 阶段结束。** 文档的打开→编辑→保存→正常关闭→重开未完成，两份文档副本仍与原合成输入同哈希。不能用许可窗口截图代替工作流通过，也不能把保护中止算作有效产品故障。下一次 GUI 执行需要适合该完整负载的机器/经明确批准的新资源方案及新的准入，禁止本轮继续自动复试或关闭阈值。

## 代码与离线验证

- 修正固定实例监督器在命令非零退出时仍返回成功的问题。离线和固定实例监督器共用 `tools/test_runtime.py::result_exit_code`，保留原生命令退出码，区分成功、命令失败与保护/证据无效。
- 增加可选客体回收线：只写当前核验过的执行子组，不改容器 HostConfig、祖先/宿主配置或硬限额。错误档位、身份替换、符号链接以及放宽已有更紧值均拒绝。该变更默认关闭，重试结果不能作为 GUI 修复成功。
- 全仓检查 [offline/command.log](offline/command.log)：867 项，5 项条件跳过，84.085 秒，原生退出 0；峰值 424.62 MiB。它在本轮后续两个小改动之前完成。
- 退出判定改动后 [protection](protection/command.log) 49 项通过；客体回收线改动后 [watermark_contracts](watermark_contracts/command.log) 35 项通过，均退出 0 且无保护原因。没有无新理由重复全仓。
- 只读/正式恢复使用 T1 硬预算，但恢复仍属于真实状态收尾；保护器的通用 `T1 offline only` 标签不能把恢复操作当作纯 fake 测试。运行时白名单和具体动作见恢复回执。

本轮代码改动的原始副本在 [before](before/) 和 [before-watermark](before-watermark/)，文档修改前副本在 [before-docs](before-docs/)。开场源码/索引指纹见 [before.json](before.json)。没有修改既有产品 `service.py/runtime.py/on_demand.py`，已有 fuse/shortcut 等脏修复保留。

## 尚未收口的专项

| 范围 | 阻断及下一步 |
|---|---|
| PRE01/04、AR03→AR01→AR02 | 新 Arch generation 2 未启动；296 包摘要和升级估算属于旧根。先建立新根真实包清单/keyring/完整升级预算，解决同根观察前置，不能沿用旧基线或做部分升级。Debian 主线 GUI 尚未完成，本轮不提前启动 Arch |
| PRE02 | 固定 Debian 根只有一个环境，不能凭全局其他容器未启动假称覆盖同根分支。真实同根第二环境、运行态一致快照、Arch/RPM 停止态清单和存储引用对账仍缺；不删登记或另建根绕过 |
| PRE03 | 本轮三 CLI 共享组、保护合同及真实中止收尾有证据；GUI/Arch 的完整覆盖未通过，不作跨负载通用放行 |
| DEP07 | 缺可信签名、独有 SONAME 的兼容/不兼容升级仓库夹具及 A/B 实际加载证据。先准备并审查仓库/签名/预算；不替换公用系统库 |
| DEP11 | 现有 ELF 自动补库不自动修复 gio 等脚本命令和桌面服务。缺口识别只能记 PARTIAL/feature_gap；本轮 GUI 尚未验证实际 MIME/界面作用 |
| DEP12 | 缺客体加载器的私有搜索路径、符号版本、错误架构及厂商配置分类完整实证；宿主 ELF 小样本不能充当目标客体验收 |
| DEP13 | 缺固定实例、独有小包、可备份恢复的单次中断方案和明确故障注入批准。不对唯一共享原生包库随意 SIGKILL；APP 租约恢复不能替代 pkg recover 验收 |
| END01 | 运行收尾通过，但开场未保存新鲜 KWin 身份、非目标逐文件基线和全局 CID 清单，完整保全验收仍 PARTIAL。这些缺测不事后补成开场事实 |

## 收尾与交付边界

[final-audit.json](final-audit.json) 和 [153 条全时段事件](final-events.jsonl) 记录：本轮登记的 29 份进程身份均已结束，所有工作/保护单元无 MainPID 和 cgroup，短临时目录已清除，专用测试 slice inactive。固定 Debian 停止，租约/bridge 空，包恢复标记为 false；登记包摘要仍为 `9a790f9e363a6ec638d98beb20f3b8367e2666865b83194822ab494520e5719c`、691 包。没有包事务写入。

当前容器总数 11，全部未运行，新 Arch 保持 created，其他实例为 exited。全时段零 create、零非目标 start/restart/exec/remove。Git 索引条目、boot、已存档的原脏源码和合成输入保持；没有停止用户应用、访问宿主桌面或修改全局资源设置。保留新增测试 APP 注册、文档副本、私有配置和历史中止材料以便复核，不做自动清理。

只有工作树及测试证据变化。没有提交、构建、发布、部署或替换日常 v0.0.4；本轮也未推进扩大测试。机器台账为 [case-ledger.json](case-ledger.json)，后续入口为 [测试剩余工作](../../docs/测试剩余工作-20261109.md)。
