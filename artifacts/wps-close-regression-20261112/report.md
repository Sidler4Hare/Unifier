# WPS 关闭问题修复与 SH10 验收

结论：**工作树修复通过，SH10 在本轮固定 Debian 条件下 PASS**。WPS 整合模式下两轮编辑/保存、两次重开、三次关闭均完成；并行 CLI 始终保留，最后租约释放后原容器自动停止。扩大测试和一期整体仍为 PARTIAL。

## 问题与修复

旧失败表现为窗口消失但启动器等待25秒不结束。诊断取证表明：前台 shell、整合界面和 Writer 进程退出后，CEF/browser 与 cloud 辅助树仍被 subreaper 收养。租约等待全部后代退出本身是正确保护，缺口是没有针对该前台命令的辅助进程退出策略。诊断轮在重开请求后触发 ABORTED_PSI，原中止锁保留；不将 PSI 单独归因于这些辅助进程。

新增显式 `app add --on-demand --exit-policy stop-children`：登记命令结束后，向该执行仍拥有的辅助进程发送 TERM，PID/starttime/pidfd 校验和 ECHILD 终态证明全部保留。默认 `wait` 不变。策略纳入 handler 指纹；旧默认指纹兼容。10秒后实际使用KILL时，原主命令0也返回125，记录原始主退出码和强制升级标志。本轮WPS未改为独立模式、未禁用云服务，也未按进程名清理。

仅适用于启动命令本身会等待界面结束的应用。该版本 `/usr/bin/wps` 的脚本和前后执行树提供本轮依据；对提前返回的后台启动器不能启用。补丁见[fix.patch](../wps-close-fix-20261112/fix.patch)，根因见[diagnosis.json](../wps-close-fix-20261112/diagnosis.json)。

## 验证

- T1定向88项通过，覆盖默认 daemon 生命周期、opt-in前台退出、并行执行隔离、真实FD3传递、强制升级返回失败、指纹/注册/无副作用拒绝与保护器合同。
- 全仓906项：901通过、5条件跳过，检查命令退出0。既有Python/SQLite/GLib警告保留于日志，未将跳过计为通过。见[完整检查](../wps-close-fix-20261112/full-check/command.log)。本地Qwen在有界期限内未交付修改，已停止，由主控完成并独立验收。
- 真GUI：两轮标记各出现一次，原文保留；重开截图和原生DOCX内容共同核验。三条独立token终态均为主命令0、执行0、all_children_reaped=true、cleanup_escalated=false。关闭动作连同查询耗时约9.00/9.22/8.48秒；CLI PID/start保持、计数703→1311→1698。见[工作流分析](workflow-analysis.json)和[最终重开画面](writer_cli/10-dismiss-default.png)。
- 汇总原预算1GiB、384任务；客体768MiB、256任务。本轮峰值251任务、约799.9MiB，宿主最低可用约3.82GiB，full PSI最高2.30%、some2.48%；无OOM、硬限额事件或保护中止。监督回执原始命令退出0，见[receipt.json](writer_cli/receipt.json)。

## 收尾与范围

最终租约自然清空，无须 `app recover`。已恢复客体192/汇总256，全部本轮进程退出。本轮各阶段共256个登记宿主身份退出；11个CID不变，非目标运行状态、原输入、包清单摘要/代际、26个原应用登记、索引、boot/KWin身份保持。仅增加本轮测试应用登记和合成证据，不删除历史数据。见[保全审计](final-audit.json)与[配额恢复](task-limits-restore.json)。

字体缺失提示及部分中文排版仍存在，字体/版式没有验收；账号/云同步、其他目标与长期GUI压力不在本次通过范围。PRE03仅补齐本轮固定Debian GUI负载，不放行Arch或默认任务预算。完整一期仍缺台账所列Arch/RPM、同管理根和包事务等项，见[最新台账](case-ledger.json)。

**本次未部署、提交或推送。** 收尾只读核对发现日常入口实际指向 `0.0.5-98f0a9546633`，不含本次策略；此前交接中的v0.0.4说法已过期。本轮修复通过指工作树和隔离实测，不能视为日常安装版已修复。见[安装入口核验](installed-entry-audit.json)。
