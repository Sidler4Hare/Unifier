# SH10 续跑：粘贴保存通过，关闭阶段超时

2026-11-12。用户降低其他负载后明确要求继续，复用原固定Debian CID完成了本次尝试。**SH10整体FAIL（正常退出未完成），第一轮私有粘贴和保存PASS；扩大测试及第一阶段仍PARTIAL。** 本次没有资源保护中止。

## 实际完成程度

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 保护器新尝试绑定与合同 | 50项通过，退出0 | contracts/command.log、receipt.json |
| 任务配额准备 | PASS，固定客体256、汇总384，其他预算不变 | task-limits-enable.json、writer_cli/live-binding.json |
| 准入与Writer/CLI同实例运行 | PASS（本段范围），原CID一次冷启动、CLI保持心跳 | writer_cli/receipt.json、live-binding.json、03-edit-one.reply.json |
| 第一轮粘贴、保存与原文校验 | PASS；正确标记恰好一次，原文保留 | writer_cli/03-edit-one.png、03-edit-one.reply.json、workflow-analysis.json |
| 第一次正常关闭 | FAIL；窗口消失，但启动器25秒未退出 | writer_cli/result.json、failure.png、command.log |
| 第一轮重开、第二轮完整循环 | NOT_RUN | writer_cli/result.json |
| 正式app recover | PASS，3条租约释放 | writer_recovery/result.json |
| 原配额与执行收尾 | PASS_SCOPED；恢复192/256，44个登记身份退出 | task-limits-restore.json、final-audit.json |

## 第一轮输入修正的实际证据

使用私有Xvfb :1115与其私有CLIPBOARD。选择请求2次、短文本传输1次，保存的DOCX含 `UNIFIER-SH10-ROUND-1` 恰好一次，没有前轮连续按键输入中的重复/漏字。原合成文档文本完整保留，保存文件SHA256为 `2398524584f6213f4d8342b4a2426d3721ad0c1cc5c245ac7b9f8e7548b14d45`。

同一时刻CLI客体PID/start保持为15/13815358，计数已增加到436；容器StartedAt保持冷启动值。保存截图同时可见原表格和新增标记。该结果验证这次短文本粘贴和保存，不证明所有输入场景或原来的字体/版式尾项均通过；截图仍显示既有Font Missing提示。

## 关闭阶段发现

驱动向准确的 `shared.docx - WPS Office` 窗口发送WM_DELETE_WINDOW，然后等待正式 `app open shared1115-writer` 启动器正常结束。25秒后等待超时；失败截图是空白的私有X画面，但同一时间最后的已核验进程覆盖仍包含 `wpsoffice`、CEF辅助进程和 `wpscloudsvr`。没有正常退出码，尚未进行重开或第二轮编辑。

原始驱动退出1，监督器reason=null并返回失败，不是ABORTED_TASKS、OOM或内存准入问题。工具服务在此阶段发生中断；恢复后读取现成回执确认独立systemd监督器已完成停止及rescue，并未重复发送关闭或重启测试。不能仅根据工具中断推断应用超时根因。

只读代码核对表明 `src/unifier/application_leases.py` 的执行等待以完整客体执行结束证明为依据，仍有后代存活时保留租约；`on_demand.py` 的release也拒绝释放未核验退出的执行。因此窗口消失本身不能充当正常退出证据。现有证据尚不能区分WPS后台驻留策略、正式退出动作与Unifier应用集成行为的责任边界；下一步应先厘清该关闭路径，不把这次流程判为通过，也不以放宽等待或资源条件掩盖它。

只读检查本次私有配置的原生UpperDir路径未发现持久配置目录，没有挂载、启动容器或从其他用户目录猜测设置；记录在 private-config-inventory.json。没有据此编造WPS退出参数或修改产品租约规则。

## 保护与资源

汇总1GiB RAM/128MiB swap/384任务/1CPU；客体768MiB RAM/128MiB swap/256任务、memory.high512MiB；控制器384MiB/128任务；独立监督64MiB。真实cgroup祖先链已核验。

峰值838578176字节（约800MiB）、260任务，宿主可用最低4238454784字节（约3.95GiB）。PSI峰值full2.39%、some2.60%；OOM、memory.max、pids.max事件增量均0。实际负载到关闭超时为止，不能外推完整两轮或Arch的PRE03。

## 收尾与保全

监督器及rescue精确停止原CID后，受保护的正式app recover释放3条租约；其runtime只允许inspect/info。恢复配额命令退出0，保存的客体Pids为192、汇总Tasks为256，专用slice inactive且零任务。停止态inspect仍保留旧OCI缓存256，恢复结论同时核对已保存配置，不以缓存覆盖保存值。

44个登记PID/starttime身份全部退出，相关单元MainPID=0、cgroup无活动任务。容器11→11、完整CID集合一致；非目标State/Pid/StartedAt/Restarts未变，167条采集事件无create或非目标执行。boot/KWin、Git索引条目、原输入、既有APP注册、包清单摘要/计数/content_revision均未改变，lease/bridge为空、无需包恢复。

新增shared1115三条注册、合成副本和失败证据保留；没有非目标逐文件快照，完整END01仍PARTIAL。源码只有本轮保护器档位绑定两行变化，测试/产品源码其余开场指纹不变；50项合同是本轮结果，旧全仓检查不冒充重跑。未新增容器、包事务、部署、提交或推送。没有自动追加GUI尝试。
