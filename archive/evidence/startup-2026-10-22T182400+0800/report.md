# GUI 拉起与生命周期测试

2026-10-22，Fedora 44 KDE/KOS、KWin 6.7.5，基线 `8482968`。测试的是三套独立环境中的真实原生 Wayland Mousepad，原腾讯会议及旧 GUI 环境保留原状。生产模块 `src/multilinux/` 未修改。

**判定：常规启动与并发租约可用；停止、快速重开和异常回收需要修复。** 机器核验中的 `measurement_valid=true` 表示证据有效，`product_verdict=needs-lifecycle-fixes` 表示策略仍有实际缺陷。不能将这轮概括成“性能全部通过”。

## 计时结果

18:29–18:47 串行完成 180 个正式样本，每个环境的冷、热启动各 30 次，另有每组一次预热，共 6 次。全部正式样本都验证了目标文件未改变、真实窗口与缓冲、正常退出和对应的环境状态。

| 环境 / Mousepad | 冷启动 p50 / p95 | 热启动 p50 / p95 | 关窗至 KWin 移除 p50（冷组） | 关窗至受管环境完整回收 p50 |
|---|---:|---:|---:|---:|
| Debian 13 / 0.6.3 | 0.871 / 0.911 s | 0.592 / 0.708 s | 50.6 ms | 8.372 s |
| Fedora 44 / 0.7.0 | 0.969 / 0.996 s | 0.672 / 0.954 s | 47.0 ms | 8.358 s |
| Arch / 0.7.0 | 0.891 / 0.943 s | 0.586 / 0.711 s | 46.9 ms | 8.376 s |

热组由测试工具预先保持环境和代理运行，每次仍启动新的私有 D-Bus 与 Mousepad 进程。热组关窗至启动器退出的 p50 为 132–144 ms；它按设计保留环境，不是完成整环境停机。产品当前没有自动预热或短时保温策略。

冷组指**容器和代理停止后重开**，不含安装、镜像准备，也没有清空宿主页缓存。计时起点是宿主启动真实 `ml.py app open` 子进程前；终点是“目标 KWin 原生窗口出现”与“该窗口对应的非空 Wayland buffer commit”两者都出现。初始空 commit、光标和其它 toplevel 不计入。180 个样本也都观察到了相应的 frame callback。

这仍然是界面就绪的代理指标，**没有测量物理屏幕呈现或动画结束**。日志、观察器和轮询开销包含在结果内。参见 [summary.json](summary.json)、[原始样本](samples/debian13/cold-measurement-000/result.json)、[计时源码](measurement-source/tools/probe_startup.py)。

## 实际发现

### 1. 停止等待 8 秒，并阻塞快速重开

容器 PID 1 是 `/usr/bin/sleep infinity`；现场 `/proc` 显示 namespace PID 为 1、`SigCgt=0`，没有信号处理器。Podman 事件记录了退出码 **137**。产品调用 `podman stop --time 8`，环境租约锁在停止过程中持续持有。

关窗后约 183–196 ms 发起新一次打开，三套环境各复测 3 次：新的窗口就绪耗时 **8.942–9.101 秒**。启动时已核对前一个容器处于 `stopping`。这解释了为什么普通冷启动约 0.9 秒，而快速重开反而约 9 秒。

证据：[PID 1 状态](stop-process-diagnostic.json)、[Podman 停止事件](stop-events-diagnostic.json)、[Debian 快速重开](diagnostics/debian13/reopen-0/result.json)。[Podman 文档](https://docs.podman.io/en/latest/markdown/podman-stop.1.html)说明停止超时后的强制终止；[Linux PID namespace 手册](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html)说明 PID 1 的信号处理限制。

### 2. 保持运行时累积僵尸子进程

Arch 在 1 次热组预热和 30 次正式打开结束后，PID 1 下有 **155 个直接子进程，全部为 Z 状态**。该快照在最后一个计时样本保存后、测试工具最终停止容器前采集，没有插入正式计时区间。

这证明当前 PID 1 不承担孤儿子进程回收职责。停止实验环境后这些进程已清除；不能把这种积累视为已经通过的长期保温策略。证据：[warm-tail-processes.json](warm-tail-processes.json)。

### 3. 强杀启动器后，自动启停所有权丢失

三套环境都复现了相同流程：

1. 从停止状态通过受管入口打开，租约记录 `auto_stop=true`。
2. 只对本次拥有的启动器 PID 发送 SIGKILL，保留实际窗口；观察两秒。
3. 关闭遗留测试窗口，环境和代理仍未自动回收。
4. 再正常打开并关闭一次，旧失效租约被清除，但 `auto_stop` 被改为 false；环境继续运行。

后续实验清理显式停止了这些自建环境。证据：[Debian](diagnostics/debian13/killed-launcher/result.json)、[Fedora](diagnostics/fedora44/killed-launcher/result.json)、[Arch](diagnostics/arch/killed-launcher/result.json)。

### 正常并发对照

三套环境各做一次真实双窗口对照：两个租约共用环境，先关闭一个窗口时另一个继续运行，最后关闭时环境和代理回收，均通过。强杀后的问题与正常并发路径分别记录。

一次初始并发探针在 `windowAdded` 时读到暂为 0 的 buffer geometry，测量程序过早报错。已保留在 `diagnostics/debian13/concurrent-attempt1/`，随后增加对同一 window id 的实际 buffer geometry 确认，复测通过。180 个单窗口样本在此修正前已经完成，源码快照与统计没有改写；后续诊断有独立源码快照。

## 启动入口分段

另做 Debian 冷、热各一次插桩诊断，独立于 180 个正式样本，不混入 p50/p95。结果显示在界面就绪之前已经完成：

| 路径 | Podman 调用 | 分段耗时 |
|---|---|---|
| 冷启动 | 1 info、8 inspect、1 start、1 exec（启动后 uname 检查） | 约 544 ms |
| 热启动 | 1 info、6 inspect | 约 374 ms |

这些是已经完成的调用，尚不含跨越就绪时刻的主应用 exec span。整个冷/热运行分别记录 17/9 次调用。数据支持先检查同次启动内的重复 RPC；它不授权删除容器归属和状态校验。插桩本身有开销，且与最后一次回归检查有短暂重叠，因此以上分段用于定位，不能作为新的性能基准。见 [phase-results.json](phase-results.json) 和 [原始 spans](phases/cold/podman-spans.json)。

## 空闲保留成本

额外抽样的是本轮带 keep-id、桌面桥接和 pasta 的环境，不同于早期最小镜像实验。

| 环境 | 新启动空闲容器进程 PSS | 关闭一次 GUI 后的 cgroup memory.current |
|---|---:|---:|
| Debian | 1.53 MiB | 5.75 MiB |
| Fedora | 1.61 MiB | 6.10 MiB |
| Arch | 2.26 MiB | 6.03 MiB |

关闭一次 GUI 后容器进程 PSS 回到相同基线，cgroup 另有约 4.75–4.80 MiB 文件缓存及内核计费。PSS 与 cgroup 不相加；这些数值也不是全部宿主桌面与网络辅助服务的总占用。停止后 cgroup 不存在，指标保留 null，不填零。各环境的 `diagnostics/<target>/resource-states.json` 保留原始记录。

## 验证和保留范围

- 180 个正式样本、6 个预热；15 个生命周期场景执行完成，另保留一次修正前的探针失败。
- 105 项代码回归，101 通过、4 项旧现场测试按原规则跳过。回归通过没有覆盖掉以上实际产品缺陷。
- 三个自建实验环境已停止，租约为空、桥接代理撤销，测试窗口均移除；231 个临时 KWin 脚本全部卸载，私有回调 D-Bus 名均释放。
- 三个原始 GUI 环境仍停止，腾讯会议原部署继续保持其原运行状态。宿主 RPM 包清单哈希与桌面入口名字清单前后相同。
- [机器核验](validation.json)、[回归](regression.json)、[回收核验](postconditions.json)和 `SHA256SUMS` 对结果分别作审计。

固定顺序为 Debian、Fedora、Arch，每套先冷后热。当前主机没有锁频，正式样本前的封装温度为 59–88°C、内存 PSI some avg10 为 0–1.7%。这是一台日常使用宿主上的工程测量，不能据此给发行版作性能排名，也不代表腾讯会议、GPU 编码或所有 GUI 应用的启动时间。

本轮只修改测量工具及其测试，没有修改生产启停逻辑。下一步优先解决 PID 1 的终止信号与子进程回收、异常租约所有权恢复，再减少启动入口的重复调用，并以同一套场景复验。短时保温应在这些问题修复后评估。

## 复现与接手

本次从三个已停止且装有 Mousepad 的环境按精确 ID 创建本地镜像快照，再创建独立实验环境，没有重新安装宿主软件。镜像身份、应用版本、包清单和冻结产品源码在本目录。初始准备采用分步命令；提供的 `tools/prepare_startup.py` 实现等价的可复用准备流程，本轮没有再创建第二套环境重复运行整个准备脚本。

```bash
# 为重测选择一个新的输出目录；原始源环境必须存在且停止。
python3 tools/prepare_startup.py --run-dir /absolute/path/to/new-evidence
python3 tools/probe_startup.py measure --run-dir /absolute/path/to/new-evidence --samples 30
python3 tools/probe_startup_failures.py --run-dir /absolute/path/to/new-evidence
python3 tools/probe_startup_phases.py --run-dir /absolute/path/to/new-evidence

# 本轮保存证据的离线核验。
python3 tools/verify_startup_evidence.py --run-dir evidence/startup-2026-10-22T182400+0800
```

观察器和入口插桩由 DSH 的 Command Code / DeepSeek V4.1 Flash 完成，持久会话 `session-a36617c6-89f3-4e8d-82ce-8981dc67b5dd`；本机启动器在 `.lab/agents/startup-20261022T182400/`。交接副本在 [agents/](agents/delegation.json)，凭据和原始推理日志未纳入证据。
