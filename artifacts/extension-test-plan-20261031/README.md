# 扩展测试计划修订 3（2026-10-31）

以 v0.0.1 为基线，只调整计划和引用已有回执。开源 CLI 补充 1 已查询官方项目/发行版网页，增加 6 项目/18 单元及 PKG01–PKG14（含直接 apt/dnf/pacman 与 apk 未实现边界）。本轮没有运行扩大测试、全量回归、远端任务、下载或容器生命周期操作。

- [执行说明](../../设计/扩展可用性与Agent跨环境测试计划-2026-10-27.md)：按宿主准入安排应用、Agent 与文件测试，补安装回退、策略与新修复回归。
- [plan.json](plan.json)：9 个应用单元、14 组 Agent 契约、20 组文件用例、14 组 CLI 和 5 个 v0.0.1 回归专题。历史 12/12 关系仅为模板；四个当前宿主各生成 6 个共享方向和 6 个 Agent 控制单元，每宿主确定性互写 54 回放。全部新增执行结果仍为 NOT_RUN。
- [relationship-plan.json](relationship-plan.json)：18 组关系保留；同构建 Fedora/WPS 移到非 Fedora 桌面，跨机对照不称为共享 bind 或纯容器性能对比。
- [oss-cli-plan.json](oss-cli-plan.json) 与 [oss-sources.json](oss-sources.json)：开源项目、5 种已支持后缀、5 类未支持输入、包管理器命令双轨和官方来源；不下载安装包。
- [preflight.json](preflight.json)：修订前 207 项与正式包一致；记录包摘要、原暂存索引摘要、两个停止的日常容器和修改前备份。产品源码未改，计划文档修改后不再与原包逐字相同。
- [validation.json](validation.json)：矩阵数量、实际策略、门槛、引用、历史字节、源文件、暂存条目和容器保全核对。可用 `python3 artifacts/extension-test-plan-20261031/validate.py` 复查；现场有后续改动时保全断言可能不再适用。

既有服务器基础验证、四机 FUSE 和本机 FreeCAD 工作流见 [v0.0.1 报告](../v0.0.1-20261030/report.md)，属于有范围的旧运行结果，不填入新增应用通过率。旧矩阵和旧发布包保持原件。11 月具体执行日期仍未定，没有创建自动任务。
