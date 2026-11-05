# 架构修复续轮：S3 / S4

日期：2026-11-05，Asia/Shanghai。工作树修复及 T1 有界验证完成；整体仍为 **PARTIAL**，未构建、提交、部署或推送。基准 HEAD 为 `6f776a61dc599ad8f1c0a476681e892248e55a6b`，输入包含既有脏改动，不能只用 HEAD 重建本轮。

## 结果

- **F03 / S3**：APP open/exec 复用 FD 3 身份通道，以客体 Python3 subreaper 监督实际子进程树。双重 fork/setsid 的后台进程存活时保留 lease/bridge；只有收养后代全部回收的 token 终态回执才能证明执行结束。宿主 worker/代理退出不再触发无证据清扫。增加 `app leases/recover/cancel`，旧 APP lease、损坏状态和未知执行保守保留/拒绝。
- **F04 / S4**：bundled 库名候选改为 `bundled-candidates-unverified`，保留原生包事务 completed 及旧 JSON 字段；记录每对象依赖边、架构、词法 ORIGIN、RPATH/RUNPATH 和未覆盖范围。CLI 与交互安装显示实际加载仍未验证。显式可信 fixture API 与自动补库分离，不运行任意厂商 ELF。
- 修复续跑发现的两处具体误判：加载器符号版本错误伴随退出码 0 时不再判通过；取消命令确认终态后，将 lease 留给活跃 worker 采集退出码，防止提前移除导致“租约丢失”。无活跃 worker 时才由取消端释放，重复释放幂等。

详细能力和操作见 [执行所有权](../../docs/应用执行所有权.md)、[依赖分层](../../docs/自动依赖与原生安装交互.md)、[总体计划](../../docs/架构问题修复计划-20261105.md)。S1/S2 结果沿用独立的 [首轮报告](../architecture-repair-20261105T104613+0800/report.md)，没有把该轮离线结果提升成实机结论。

## 验证与原失败

[机读测试摘要](test-summary.json) 列出 **198 个唯一通过用例**，没有将重复执行相加。以下各批为串行、有独立保护的 T1；真实容器/包操作与 GUI 未执行。

| 批次 | 结果 | 证据 |
|---|---|---|
| 依赖分层与可信本机 ELF | 26 PASS | [日志](resume1/s4-recheck/command.log)、[保护回执](resume1/s4-recheck/receipt.json) |
| 执行树、恢复、取消、应用绑定 | 30 PASS | [日志](resume1/s3-valid-retry/command.log)、[保护回执](resume1/s3-valid-retry/receipt.json) |
| 原控制通道、项目会话、CLI 消费者 | 114 PASS，1 条旧契约断言 FAIL，已在下一批更新并验证 | [原日志](resume1/consumer-check/command.log)、[回执](resume1/consumer-check/receipt.json) |
| 更新后的恢复契约与生命周期 | 30 PASS，与上一批有 2 个通过用例重叠 | [日志](resume1/contract-valid-retry/command.log)、[回执](resume1/contract-valid-retry/receipt.json) |
| 仓库快速检查 | PASS：213 个 Python 文件语法、42 个旧模块别名、5 个 CLI 入口 | [同批日志开头](resume1/consumer-check/command.log) |

旧断言要求宿主 PID 失效就删除 legacy APP lease，与 S3 的客体证据规则相反。现在明确保留 APP/项目 lease，纯宿主记账 lease 仍可按 PID/starttime 过期；另验证保留的 legacy APP lease 阻止误停原组启动的容器。没有关闭原检查或改成无条件通过。

保留的失败与无效尝试：

1. [原 S4 失败](s4-acceptance/command.log)：符号版本 `UFIX_1` 缺失，但 `--list` 原生退出码为 0。修正为诊断/退出码/完成帧共同判定；未知诊断为 unverified。[有效复试](resume1/s4-recheck/command.log) 包括搜索路径外的同名库、有效 ORIGIN、错误 ELFCLASS、缺符号版本、RUNPATH 非传递、初始图成功但 dlopen 失败。
2. [取消竞态修复前](resume1/s3-cancel-before/command.log)：取消端先释放租约，worker 报租约消失。相同边界由 [有效生命周期复试](resume1/s3-valid-retry/command.log) 覆盖。
3. [第一次生命周期续跑](resume1/s3-recheck/receipt.json)：断言通过，但保护器在 cgroup 退出竞态遇到 ENODEV，样本 **INVALID**。仅在两个持久完成回执均存在后接纳 ENOENT/ENODEV，仍检查最终 OOM/pids 事件；[修正依据](resume1/s3-monitor-correction.json)，一次有依据复试通过。
4. [第一次旧契约补跑](resume1/contract-recheck/command.log)：3 个 fake proxy 使用绝对 Unix socket 路径，深层 TMPDIR 超过路径限制，记 **INVALID_FIXTURE**。使用短路径、私有 0700 HOME/XDG/TMP 复试，不接活动桌面或总线；[依据及私有目录位置](resume1/contract-valid-retry/basis.json)、[spec](resume1/contract-valid-retry/spec.json)。未修改产品的相对 socket 行为。

早先 [Qwen S4 尝试](qwen-s4/receipt.json) 未在期限内交付可验收实现；仅留下说明文字/未使用导入，没有自报即验收，也未重复启动模型。本轮主控接手实现并独立检查差异。完整原始日志和保护失败均保留。

## 保护、保全与停止

采用用户修订后的 **1 GiB MemAvailable** 保留线。每批新做 30 秒准入，计入 512 MiB worker、64 MiB 独立保护器和 128 MiB 余量；worker 为 96 tasks / 1 CPU / 0 swap，保护器 16 tasks / 1 CPU / 0 swap，250 ms 监测及独立期限。有效续跑的最低采样 MemAvailable 为 3,272,269,824 字节（约 3.05 GiB）。无 OOM/pids 硬事件；CPU 节流计数如实保留，不作性能结论。

- [全部单元收尾](units-after.json)：24 个精确登记的 worker/guardian 单元，MainPID 均为 0、无 ControlGroup；没有残留本轮受管进程。没有全局 kill、prune、reset 或停止无关应用。测试中的私有 socket 用例正常关闭；未创建显示、转发或网络监听服务。
- [保全检查](preservation.json)：Git 索引、boot ID 与输入一致；既有文件仅在明列的允许范围改变，其他基线文件哈希保持。改前副本在 [before](before/)，暂停续跑前副本在 [resume1/before](resume1/before/)；[本轮补丁](this-run.patch) 只包含这次续轮，不夹带初始脏工作。
- 实际新增/启动项目容器为 0；没有 guest 包事务、宿主安装/配置或活动桌面操作。没有重新盘点容器总数、读取账号/日常用户数据或采集 KWin 身份，不能据此填报全部日常状态已现场验收。T1 私有 fixture/日志保留用于复核，不自动清理。

## 尚未完成

| 边界 | 状态及原因 |
|---|---|
| 通用单实例/外部服务代启 | PARTIAL：子进程树监督成立，外部实例协议映射未实现。短请求不转移旧 lease；已有受管实例继续持有自己的 lease。外部单实例应用应手动预启动环境，不能将短请求退出当成真实实例结束。 |
| 实际客体 subreaper / 目标发行版 loader | NOT_RUN：本机微型进程和 ELF 是内核机制证据，不是 Debian/Arch 容器验收；不拷贝宿主库/程序到客体凑证明。 |
| PRE03 / S5 | BLOCKED：当前 T1 保护器未完成真实容器汇总 cgroup、非目标事件及全部保护注入验收，不能当作 T2 放行器。 |
| F01 同管理根真实同伴 | BLOCKED：首轮所核对固定池不满足该分支；没有新建、迁移或换根绕过。Arch/RPM 清单适配仍明确 unsupported。 |
| WPS 等真实 GUI 保存/关闭/重开 | NOT_RUN：没有操作活动桌面，GUI 前提也未放行。 |
| 构建、部署、发布 | NOT_RUN：用户未要求，日常安装版没有这轮改动。 |

[本轮 case ledger](case-ledger.json) 映射原专项 ID，单列 T1 支持证据及尚未完成的真实分支；没有改写原计划或原 NOT_RUN 回执。整体架构修复不能因 198 个离线用例通过而标 complete。

机制参考：[Linux subreaper](https://www.man7.org/linux/man-pages/man2/PR_SET_CHILD_SUBREAPER.2const.html)、[glibc rtld.c](https://raw.githubusercontent.com/bminor/glibc/master/elf/rtld.c)。glibc 的 list 模式版本检查可记录诊断而不按 normal 模式退出；本轮也保留了实际 fixture 反例，不仅依赖源码解释。
