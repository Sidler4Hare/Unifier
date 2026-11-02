# 生命周期真机验证（停止 / 快速重开 / 僵尸 / 所有权 / 并发租约）

运行目录：lifecycle-2026-10-22T212236+0800，生成时间 2026-10-22T21:28:07+08:00。宿主 Fedora 44 KDE / KWin 6.7.5，rootless Podman 5.8.9，基线提交 fd0b2da877ec6a4059b0260c8aa53695851f1fac（分支 codex/screencast-protocol，工作树脏）。本轮只新增测量工具与新证据目录；产品代码由 project/ 冻结快照决定，未改写已有证据。

**判定：needs-lifecycle-fixes，仍存在失败项，详见下文。**

机器核验：measurement_valid=true，product_verdict=needs-lifecycle-fixes。判定标准见 validation.json；不能把仅离线单测通过当成真机生命周期通过。

## 方法与环境

- 两个全新独立环境：test-debian（Debian 13）与 test-arch（Arch Linux），由上一轮 startup-* 本地镜像摘要创建，--network none --desktop --portals --userns keep-id；各自独立 work 目录。镜像摘要与 SHA-256 见 manifest.json。
- 应用：每个环境一个 on-demand Mousepad（as_host_user、GDK_BACKEND=wayland、WAYLAND_DEBUG=client、LC_ALL=C.UTF-8），窗口标题含每样本唯一 marker。
- 就绪口径：KWin windowAdded 的原生 Wayland toplevel + 客户端非空 buffer commit；不是物理屏幕呈现。全部场景串行执行；只有 F/H 按需求并发 2/3 个窗口。
- 停止时长：关窗请求 → Podman died 事件（timeNano）；另记录启动器退出、环境记录 stopped、桥接消失与 PID 1 信号处置。

## 宿主条件

- 可用内存最小值：2176.9 MiB；内存 PSI some avg10 最大 5.81%，full avg10 最大 5.63%；1 分钟 load 最大 2.82。
- 每个样本的原始宿主快照在 samples/<target>/<scenario>/*/result.json 的 host_before。宿主压力超过阈值时脚本会记录并等待，而不是继续加窗。

## A. 冷启动 → 关窗自动停止（每目标 10 次）

| 环境 | 就绪 p50 | 就绪 p95 | 关窗→移除 p50 | 关窗→启动器退出 p50 | 关窗→容器 died p50 | 关窗→记录 stopped p50 |
|---|---:|---:|---:|---:|---:|---:|
| arch | 892.3 | 1070.4 | 44.8 | 327.4 | 206.4 | 26.0 |
| debian13 | 950.9 | 1542.9 | 45.6 | 337.5 | 206.3 | 25.4 |

- arch：10/10 通过；容器退出码分布 {'143': 10}；PID 1 名称 {'podman-init': 10}；HostConfig.Init {'True': 10}。
- debian13：10/10 通过；容器退出码分布 {'143': 10}；PID 1 名称 {'podman-init': 10}；HostConfig.Init {'True': 10}。

## B. 关窗后 150–250 ms 快速重开（每目标 3 次）

- arch：3/3 通过；重开就绪 p50/p95/max = 989.0/999.1/999.1 ms（阈值 3000 ms，旧缺陷约 8.9 s）；单容器 = True。
- debian13：3/3 通过；重开就绪 p50/p95/max = 873.5/941.6/941.6 ms（阈值 3000 ms，旧缺陷约 8.9 s）；单容器 = True。

## C. 热组 30 次开关后僵尸统计

- arch：30/30 热组通过；僵尸进程 0 个，PID 1 直接子进程 1 个；原始进程表 samples/arch/C/processes-after-30.txt。
- debian13：29/30 热组通过；僵尸进程 0 个，PID 1 直接子进程 1 个；原始进程表 samples/debian13/C/processes-after-30.txt。

## D. 只 SIGKILL 顶层启动器

- arch：窗口存活 = True，关窗后自动停止 = True，随后正常受管循环恢复自动停止 = True；通过
- debian13：窗口存活 = True，关窗后自动停止 = None，随后正常受管循环恢复自动停止 = None；失败：['exception: TimeoutError: compositor never removed the closed window']

## E. 同时 SIGKILL 启动器与 worker

- arch：窗口存活 = True；异常关窗后环境未自动停属可接受行为（记录于样本 JSON），异常后的正常受管循环最终自动停止 = True；通过
- debian13：窗口存活 = True；异常关窗后环境未自动停属可接受行为（记录于样本 JSON），异常后的正常受管循环最终自动停止 = True；通过

## F. 双窗口并发租约

- arch：单容器 = True，lease=2 = True，关一个后仍运行 = True，关最后一个自动停止 = True；通过
- debian13：单容器 = True，lease=2 = True，关一个后仍运行 = True，关最后一个自动停止 = True；通过

## G. 手动预启动保留

- arch：受管关窗后环境保持 running = True；通过
- debian13：受管关窗后环境保持 running = True；通过

## H. 停止态并发 3 次首启

- arch：单容器 = True，场景内 create 事件 = 0，start 事件 = 1，三窗口全部关闭后自动停止 = True；通过
- debian13：单容器 = True，场景内 create 事件 = 0，start 事件 = 1，三窗口全部关闭后自动停止 = True；通过

## 失败项

- c_zero_zombies：{"targets": 2, "zombie_total": 0, "cycles_per_target": 30, "per_target": {"arch": {"zombies": 0, "children_of_pid1": 1, "cycles_passed": 30, "planned": 30, "failures": []}, "debian13": {"zombies": 0, "children_of_pid1": 1, "cycles_passed": 29, "planned": 30, "failures": ["only 29/30 warm cycles passed"]}}}
- d_scenario_pass：{"attempts": 2, "expected": 2, "passed_count": 1, "per_target": {"arch": {"passed": true, "failures": []}, "debian13": {"passed": false, "failures": ["exception: TimeoutError: compositor never removed the closed window"]}}, "window_survived_kill": {"arch": true, "debian13": true}, "auto_stop_after_kill": {"arch": true, "debian13": null}}

## 清理与残留核对

- all_clean=true：运行中容器 0 个，存活租约 0 个，桥接进程 0 个，遗留窗口 0 个，遗留启动器/worker 进程 0 个。
- 两个自建环境保留为停止状态以便复现；未删除环境记录、未触碰任何既有容器/环境/证据。

## 限制

- Endpoint is a native compositor window plus a non-empty buffer commit; physical display presentation is not measured.
- Single host: Fedora 44 KDE, KWin 6.7.5, rootless Podman; no frequency locking on a shared daily-use machine.
- Scenarios F/H intentionally run two/three windows of one environment at once; all other GUI work is serial.
- Container images and package inventories were frozen from the preceding startup round; no network or image refresh was used.
- Lifecycle sampling counts are 10 cold cycles, 3 fast reopens and 30 warm cycles per target, plus one pass per targeted scenario.

## 证据与复现

- 冻结源码与配置：project/（逐文件 SHA-256 与 git 状态见 manifest.json 的 snapshot）。
- 原始样本：samples/<target>/<A-H>/*/result.json、stdout.log、stderr.log、pid1-*.txt、processes-*.txt、events.jsonl。
- 机器判定：validation.json；汇总：summary.json；审计哈希：SHA256SUMS。

    python3 tools/prepare_lifecycle.py snapshot --run-dir <new-evidence-dir>
    python3 tools/prepare_lifecycle.py create --run-dir <new-evidence-dir>
    python3 tools/probe_lifecycle.py run --run-dir <new-evidence-dir>
    python3 tools/probe_lifecycle.py summary --run-dir <new-evidence-dir>

## 后续复验（保留本轮历史严格判定）

- 本轮判定保留为 **needs-lifecycle-fixes**（历史严格判定，criteria 与失败项原文未改动）。
- 最终冻结代码上的完整复测在独立证据目录 **evidence/lifecycle-2026-10-22T213032+0800** 完成，判定 **lifecycle-fixed**。
- 复验结果：Debian/Arch 热组均 30/30、僵尸 0；D 两目标通过（顶层 SIGKILL 后窗口保留、关窗自动停止、随后恢复正常自动停止）；E/F/G/H 两目标全部通过。
- 本轮两个 Debian 的 TimeoutError（C cycle-015、D）经原始客户端协议日志复核：关闭请求已送达客户端，但共享桌面上真实键盘输入把打开的文档标记为已修改，Mousepad 弹出自身 Save Changes 模态框，因此窗口未移除；不是合成器 close 竞态、不是 Mousepad 崩溃/挂起、不是产品停止流程失败。证据与诊断见第二轮目录的 **diagnostics/attempt1-interference.json**。
- 复验使用独立环境与独立样本，未覆盖、未改写本轮 C/D 原始记录；仅在本轮末尾追加本说明并使 SHA256SUMS 覆盖最终文件。
