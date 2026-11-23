# SH10 私有粘贴复试：启动准入阻断

2026-11-12。用户明确批准“继续，同额预算再跑一次”。本次 **BLOCKED_ADMISSION**：没有启动测试容器、Writer、CLI或私有显示，粘贴脚本未实机执行；不是新的输入或产品功能失败。扩大测试和第一阶段仍未完成。

## 结果与证据

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 新尝试精确绑定、保护合同 | 50项通过，原始退出0 | contracts/command.log、receipt.json |
| 停止态配额准备 | PASS，保存客体任务上限192→256，其他配置与生命周期不变 | task-limits-enable.json |
| GUI准入 | BLOCKED，60秒未形成15秒合格窗口，监督退出2 | writer_cli/abort.json、receipt.json、admission.jsonl |
| 容器/文档/粘贴工作流 | NOT_RUN，cleanup=NO_START | writer_cli/receipt.json、rescue.json |
| 恢复原配额 | PASS，保存客体192、汇总256 | task-limits-restore.json、configure-restore/receipt.json |
| 本轮收尾 | PASS_SCOPED，10个登记身份已退出 | final-audit.json |

## 为什么没有启动

沿用获批的汇总1GiB、独立监督64MiB、宿主保留1GiB、余量128MiB，总准入线为 **2348810240字节（2.1875GiB）**；另要求连续15秒PSI full<2%、some<5%。开场只读查询时宿主可用约3.19GiB，实际等待准入期间变动到约2.08–2.35GiB，241次样本中145次低于内存门槛，最长合格窗口仅3.26秒。

等待60.12秒后保护器记录 `admission failed`。PSI峰值full0.25%、some0.35%，本次阻断是可用内存不足以持续准入；不将未启动的作业内存峰值0解释为真实负载测量。统计见 admission-analysis.json。未关闭用户应用腾内存，未放宽门槛，未删除锁或自动再试。

## 代码与授权边界

仅将 `tools/reuse_test_guard.py` 的显式任务档位改绑本次 `sh10-20261112-input` / `unifier-expanded-1112-writer-input-r1`；默认256/192不变。旧档位不再用于新启动。增量两行见 this-turn-only.patch，原件在 before/。采用前轮准备的私有Xvfb :1114粘贴方案，文件已复制并做语法检查，但GUI没有准入，不能声称输入修正已验证。

50项是本轮受影响合同；前轮全仓结果不冒充本轮重跑。没有产品代码改动、编译、包事务、部署、提交或推送。执行范围与资源预算见 execution.md，前后指纹见 before.json、tested-files.sha256.json。

## 收尾

本轮10个登记PID/starttime身份全部退出，相关工作/监督单元MainPID=0；专用slice inactive，任务数0。配额恢复命令退出0，保存配置Pids192，汇总Tasks256。停止态inspect缓存仍为256，最终同时用只读保存配置和cgroup核验，不把上次OCI缓存当作本次保存配置。

11→11且完整CID集合相同；包括目标在内，所有容器State/Pid/StartedAt/Restarts未变化，目标完整State也未变化。没有start-intent、go、私有根或shared1114输入副本，APP注册无新增或修改。原租约/bridge为空且文件内容未变化，无需执行app recover。原文档、环境记录、boot/KWin身份、Git索引条目均保持；受保护源码基线只有上述保护器绑定变化。

原生事件采集尚未启动，因此不虚构“采集到0个非目标事件”的证据；上述保全结论来自开场/结束只读核对与未发起工作流的执行证据。不包含非目标逐文件内容审计，完整END01仍PARTIAL。

SH10最后一次真实工作流仍是前轮的输入文字失真，完整两轮保存/正常关闭/重开尚未验证。本次没有再次要求增加资源预算；后续仍须宿主满足同一准入条件，并保留本轮阻断记录后登记新的执行。
