# 批准任务配额后的 SH10 复试

2026-11-12。**整体仍 PARTIAL。SH10 输入保真检查 FAIL，归因未定；没有保护中止。** 用户批准了前轮的单次任务配额方案。本轮执行了该次复试，原配额已恢复，未新增容器、部署或提交。

## 验证结果

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 精确任务档位合同 | 49项通过 | contracts/ |
| 补充停止态配置读取后合同 | 50项通过，退出0、无保护中止 | contracts-config/ |
| 客体256 / 汇总384真实生效 | 已核验祖先链，其他预算不变 | writer_cli/live-binding.json、test-slice-ready.json |
| Writer＋CLI共用实例 | Writer打开并保存了修改；CLI同时运行；标记不一致，原始退出1 | writer_cli/result.json、command.log、input-evidence.json |
| 两轮正常关闭和重开 | 未执行，SH10不能判通过 | writer_cli/result.json |
| 正式app recover | 3条租约释放，只允许inspect/info | writer_recovery/result.json |
| 原配额恢复 | 保存配置客体192、汇总256；测试组inactive且零任务 | task-limits-final-verified.json |
| 执行收尾 | PASS_SCOPED，58个登记身份均退出 | final-audit.json |

上一轮892项全仓（5跳过）是本轮保护器修改前的证据。本轮只跑了受影响的保护器合同，不将旧全仓结果当作本轮重跑。

## 输入失败与资源证据

复用原固定Debian完整CID、管理根、UUID/generation，使用私有Xvfb :1113、HOME/XDG/IPC及合成文档副本。通过许可窗口并关闭默认应用提示后进入实际文档，截图在 writer_cli/02-dismiss-default.png。第一次 `xdotool type --delay 25` 后保存的字符串为 `NNNNNIFFIEER-SH10-ROOUND-1`，预期是 `UNIFIER-SH10-ROUND-1`；等待25秒仍不一致，驱动退出1，保护器按原始失败返回1并精确收尾。没有把DOCX直接改成期望内容，也没有运行第二次GUI。

峰值259任务、838840320字节内存（约800 MiB）；宿主可用最低2709585920字节（约2.52 GiB）。PSI峰值full2.75%、some3.10%；OOM、memory.max、pids.max事件增量均0。receipt.reason为null，因此该次不同于前轮ABORTED_TASKS。该数据证明这段负载未触及保护，不证明完整两轮或Arch的PRE03已经完成。文本输入路径为何重复/漏字尚未隔离到自动化、X事件处理或应用，不能直接宣称产品故障已定位。

## 保护适配与准备校验

仅修改 `tools/reuse_test_guard.py` 及其合同测试：默认256/192保持；新档位绑定此次单元、完整CID、管理根、UUID/generation和600秒期限，启用384/256。集中launch入口持有同一测试锁设置专用slice并启动监督，所有原保护条件保留。测试前文件备份在before/，本轮相对开场增量见 `this-turn-only.patch`；现有脏改动保留。

本机Podman5.8.9更新停止态资源后，保存配置已经是256，inspect仍读上次OCI文件的192。初次准备命令因此返回1，没有启动容器。只读核验SQLite精确CID记录后，保护器在停止态读取保存的资源并核对内存/CPU/身份；运行态继续验证inspect及真实cgroup。没有直接写数据库、删OCI文件或放宽运行态检查。该行为与[Podman更新实现](https://github.com/containers/podman/blob/v5.8.9/libpod/container_internal.go#L2663-L2675)和[specFromState读取实现](https://github.com/containers/podman/blob/v5.8.9/libpod/container.go#L366-L386)一致。

恢复原生写入192已成功；两次收尾验证命令错误地要求inactive slice的ControlGroup路径必须消失，故保留退出1回执。最终只读审计确认该专用组inactive、pids.current=0、populated=0、无子组/进程，TasksMax256；保存客体配置192，inspect缓存仍为256。后续核验不能以缓存数值覆盖已保存配置，不能把空cgroup目录当作活进程。原验证错误未隐藏，helper已修正但未再执行native恢复；最终证据见 task-limits-final-verified.json 和 final-audit.json。

## 保全与剩余边界

58个登记PID/starttime身份均已退出；工作/监督单元MainPID=0，本轮各cgroup无活任务，固定CID stopped、lease/bridge为空、无需包恢复。11→11，CID集合相同，非目标状态/PID/StartedAt/Restarts无变化；128条原生事件无create、无非目标执行。boot/KWin、Git索引条目、原文档、既有APP注册、包清单摘要/计数/content_revision均未变。保留新增shared1113注册、私有文件和失败证据，不删除历史数据。没有非目标逐文件内容快照，故不宣称完整END01保全验收。

已准备下一次私有剪贴板输入脚本，只有静态审查/语法通过，未实跑。具体动作、同额预算、验收和恢复见 [下一次方案](next-execution-proposal.md)。本次单次范围已用完，尚未再次启动GUI。第一阶段仍缺SH10完整工作流、Arch/RPM清单与真实事务等既有门槛；扩大测试没有全部完成。
