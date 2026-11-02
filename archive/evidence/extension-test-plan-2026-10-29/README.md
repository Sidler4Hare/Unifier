# 扩大测试计划修订 2（2026-10-29）

这是计划与只读预检记录，没有新增应用、Agent、文件系统或服务器测试通过结果。

- 人工执行说明：[修订计划](../../设计/扩展可用性与Agent跨环境测试计划-2026-10-27.md)。保留原文件入口，正文已更新至 10 月 29 日代码。
- [plan.json](plan.json)：保留 9 个应用安装单元、12 个数据方向、12 个 Agent 目标组合、14 组 Agent 契约、20 组文件系统用例；新增 CLI01–CLI12，按子例记录工程依赖。
- [relationship-plan.json](relationship-plan.json)：保留 18 组同名/同类型用例，分别记录宿主原生对照与产品路由的前置条件。
- [preflight.json](preflight.json)：123 文件与 CLI 冻结指纹一致；只读观察本机两个日常容器；记录旧输入哈希及本次文档修改前备份。
- [validation.json](validation.json)：计划单元/引用/依赖/历史输入和源码保全核对。不是全量回归或扩大测试执行结果。

用户服务器上的三个真实宿主已开测，状态为 IN_PROGRESS_USER_REPORTED；尚未导入每台身份、源码版本或结果。整套扩大测试为 2026 年 11 月 SCHEDULED_NOT_STARTED，具体日期未定，原因 DEFERRED_BANDWIDTH。本次未建立自动调度，未访问服务器或下载、安装、启停容器。

当前基线为 5e13d0d 加未提交的新 CLI；[CLI 冻结记录](../cli-installation-2026-10-29/source-frozen.json)与[验收报告](../cli-installation-2026-10-29/report.md)是本次引用的机制证据，不填入新工作流通过率。10 月 28 日便携包不代表本版。后续运行必须保存实际逐文件指纹和独立 UNIFIER_HOME。

[10 月 27 日计划](../extension-test-plan-2026-10-27/plan.json)、[来源](../extension-test-plan-2026-10-27/sources.json)、[原关系矩阵](../installed-applications-2026-10-27/relationship-plan.json)未改写。历史版本、默认关联、安装包路径与来源快照须在执行时重新确认；本次没有联网刷新。
