# 扩测续跑回执

2026-11-12（Asia/Shanghai）。总体 **PARTIAL**，当前可执行的 GUI 阶段因任务数保护结束。第一阶段未完成：除扩测外，当前工作树仍缺 Arch/RPM 停止态清单、同管理根同伴/运行态一致快照和三目标包事务等门槛。不是只差再跑一遍测试。

使用当前工作树 `c947077f127cf142c78796bf64d6d1542c1ab091` 加原有脏修改；不混用日常已安装版本或其他工作树。开场源码、索引、宿主及11个容器基线见 before.json、git-index-before.sha256、fixed-debian-before.json。

## 本轮结果

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 首次全仓 T1 | FAIL；891项，1失败、5跳过；无保护中止 | offline-baseline/receipt.json、command.log |
| 本地 Qwen 窄任务 | INVALID；300秒工作单元期限到达，未产出文件 | qwen-fix/receipt.json、command.log |
| 主控修复后定向回归 | PASS；87项、退出0 | bridge-contracts/receipt.json、command.log |
| 修复后全仓 T1 | PASS；892项（887通过、5条件跳过），退出0 | offline-after-fix/receipt.json、command.log |
| PRE03-GUI/SH10 | INVALID / ABORTED_TASKS；未完成文档编辑保存重开 | writer_cli/receipt.json、samples.jsonl、initial.png |
| 正式租约恢复 | PASS；3条租约释放，未启动/exec容器 | writer_recovery/result.json、writer-recovery-guard/receipt.json |
| 本次执行收尾 | PASS（范围见下）；完整扩大矩阵/END01仍PARTIAL | final-audit.json |

## 唯一代码改动

`tests/test_agent_bridge_recovery.py` 的“已退出工作进程”夹具实际仍引用当前测试进程。产品按存活worker保留恢复阻挡是正确的。测试现在用不匹配的进程启动标识模拟原worker已退出/PID复用，并明确核对worker/child不存活、guest已结束、lease为空；另加死broker但worker仍存活时不恢复、不重放、保留ledger阻挡的对照。

产品源码未改，没有放宽恢复条件。修改前原件在 before-code/，本轮增量在 this-turn-test.patch。Qwen经过最小overlay和脱敏dump确认 local-server / swift-1.5-iq3_s / max；工作单元 Result=timeout 后监督器读取已退出PID产生ENOENT，故回执 INVALID，不冒充功能失败。该任务整组和临时目录已清理，没有重试模型；修改由主控完成。

## GUI 实际经过与保护

复用既有 Debian CID `e58754f0645ae1ff2924cb09a7264cc821e44893e7589a7fd0ebca09ce5f5e45`，generation 1，原管理根不变。Xvfb :1112、私有HOME/XDG/IPC、软件渲染、合成文档副本，不连接宿主桌面/账号/设备。原有许可窗口截图可核验；发出本次私有许可确认操作后，在继续启动和查询窗口时触发保护，未取得编辑/保存/正常关闭/重开证据。

真实覆盖记录含init/conmon/控制器/客体祖先链。汇总上限1 GiB/128 MiB swap/256 tasks/1CPU，客体原768 MiB/192 tasks，控制器384 MiB/128 tasks，独立监督64 MiB。沿用11月11日已批准的PSI/内存持续时间规则，未增加硬预算。

峰值约800 MiB、250任务；末样本客体189任务、控制器61任务。宿主可用最低2.58 GiB，末样本full PSI 1.00%、some 1.01%；没有OOM、内存硬事件或pids.max事件。两次采样超过任务数90%后ABORTED_TASKS；该停机后驱动遇到“cannot bind a stopped execution”，是保护之后的连带失败。监督器返回2，外层bootstrap因待恢复租约返回1；二者均保留，不能解释为Writer有效功能故障。

保护精确停止原CID，独立rescue也记录STOPPED。随后正式app recover释放3条已证实结束的租约；恢复runtime只允许inspect/info，禁止start/exec/stop，没有删除中止锁。此阶段不自动重试。具体待批准的任务数方案见 next-execution-proposal.md；它尚未实施。

## 收尾与保全

final-audit.json：55个已登记进程身份全退出，相关工作/监督单元无MainPID和残留cgroup，独立测试slice inactive。固定Debian exited、租约与bridge为空，package_recovery_required=false；包摘要及原始文档不变。新私有文件、shared1112三条注册和全部失败证据保留以便复核。

容器11→11，CID集合一致；49条采集事件无create或非目标执行。原日常会议/钉钉容器保持running，其他非目标状态不变；宿主boot/KWin身份未变，Git索引条目未变，开场源码清单只有上述测试文件变化。未做包事务、下载、部署、提交、推送或日常应用清理。没有递归哈希非目标用户目录，故不宣称逐文件完整保全审计或整个END01已补齐。

## 后续仍缺什么

- 当前这台机器：SH10/GUI因任务数预算阻断；Arch还未进入完整升级、同实例复用和依赖复核。
- 当前产品：runtime.py的只读清单适配明确仅支持apt；Arch/pacman、RPM及运行态一致快照仍unsupported。recover-storage入口已在既有脏工作树中，不等于真实恢复已验收。
- 夹具/授权：同管理根第二个合规环境，DEP07独有签名升级仓库，DEP13单次安全包事务中断方案及明确故障注入批准。
- 原扩大矩阵：其他宿主厂商应用、账号/会议/设备、原生Agent矩阵、文件语义、GUI版式尾项和本机长测。远期规划已把部分项目排在三目标之后；不能把它们与一期的六项门槛混算。

机器台账 case-ledger.json 保留11月9日历史状态并增加本轮尝试；没有把旧PASS记成本轮重测，也没有把保护中止转为PASS。
