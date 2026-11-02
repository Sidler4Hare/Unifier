# 生命周期真机验证（停止 / 快速重开 / 僵尸 / 所有权 / 并发租约）

运行目录：lifecycle-2026-10-22T213032+0800，生成时间 2026-10-22T21:37:29+08:00。宿主 Fedora 44 KDE / KWin 6.7.5，rootless Podman 5.8.9，基线提交 fd0b2da877ec6a4059b0260c8aa53695851f1fac（分支 codex/screencast-protocol，工作树脏）。本轮只新增测量工具与新证据目录；产品代码由 project/ 冻结快照决定，未改写已有证据。

**判定：lifecycle-fixed，生命周期修复在该场景集上成立。**

机器核验：measurement_valid=true，product_verdict=lifecycle-fixed。判定标准见 validation.json；不能把仅离线单测通过当成真机生命周期通过。

## 方法与环境

- 两个全新独立环境：test-debian（Debian 13）与 test-arch（Arch Linux），由上一轮 startup-* 本地镜像摘要创建，--network none --desktop --portals --userns keep-id；各自独立 work 目录。镜像摘要与 SHA-256 见 manifest.json。
- 应用：每个环境一个 on-demand Mousepad（as_host_user、GDK_BACKEND=wayland、WAYLAND_DEBUG=client、LC_ALL=C.UTF-8），窗口标题含每样本唯一 marker。
- 就绪口径：KWin windowAdded 的原生 Wayland toplevel + 客户端非空 buffer commit；不是物理屏幕呈现。全部场景串行执行；只有 F/H 按需求并发 2/3 个窗口。
- 停止时长：关窗请求 → Podman died 事件（timeNano）；另记录启动器退出、环境记录 stopped、桥接消失与 PID 1 信号处置。

## 宿主条件

- 可用内存最小值：2325.1 MiB；内存 PSI some avg10 最大 0.0%，full avg10 最大 0.0%；1 分钟 load 最大 2.16。
- 每个样本的原始宿主快照在 samples/<target>/<scenario>/*/result.json 的 host_before。宿主压力超过阈值时脚本会记录并等待，而不是继续加窗。

## A. 冷启动 → 关窗自动停止（每目标 10 次）

| 环境 | 就绪 p50 | 就绪 p95 | 关窗→移除 p50 | 关窗→启动器退出 p50 | 关窗→容器 died p50 | 关窗→记录 stopped p50 |
|---|---:|---:|---:|---:|---:|---:|
| arch | 887.2 | 1073.5 | 57.9 | 312.3 | 199.4 | 24.4 |
| debian13 | 884.1 | 1025.2 | 44.5 | 316.9 | 198.7 | 24.5 |

- arch：10/10 通过；容器退出码分布 {'143': 10}；PID 1 名称 {'podman-init': 10}；HostConfig.Init {'True': 10}。
- debian13：10/10 通过；容器退出码分布 {'143': 10}；PID 1 名称 {'podman-init': 10}；HostConfig.Init {'True': 10}。

## B. 关窗后 150–250 ms 快速重开（每目标 3 次）

- arch：3/3 通过；重开就绪 p50/p95/max = 886.0/899.2/899.2 ms（阈值 3000 ms，旧缺陷约 8.9 s）；单容器 = True。
- debian13：3/3 通过；重开就绪 p50/p95/max = 883.7/1004.8/1004.8 ms（阈值 3000 ms，旧缺陷约 8.9 s）；单容器 = True。

## C. 热组 30 次开关后僵尸统计

- arch：30/30 热组通过；僵尸进程 0 个，PID 1 直接子进程 1 个；原始进程表 samples/arch/C/processes-after-30.txt。
- debian13：30/30 热组通过；僵尸进程 0 个，PID 1 直接子进程 1 个；原始进程表 samples/debian13/C/processes-after-30.txt。

## D. 只 SIGKILL 顶层启动器

- arch：窗口存活 = True，关窗后自动停止 = True，随后正常受管循环恢复自动停止 = True；通过
- debian13：窗口存活 = True，关窗后自动停止 = True，随后正常受管循环恢复自动停止 = True；通过

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

无。所有预设判定项通过。

## 清理与残留核对

- all_clean=true：运行中容器 0 个，存活租约 0 个，桥接进程 0 个，遗留窗口 0 个，遗留启动器/worker 进程 0 个。
- 两个自建环境保留为停止状态以便复现；未删除环境记录、未触碰任何既有容器/环境/证据。

## 限制

- Endpoint is a native compositor window plus a non-empty buffer commit; physical display presentation is not measured.
- Single host: Fedora 44 KDE, KWin 6.7.5, rootless Podman; no frequency locking on a shared daily-use machine.
- Scenarios F/H intentionally run two/three windows of one environment at once; all other GUI work is serial.
- Container images and package inventories were frozen from the preceding startup round; no network or image refresh was used.
- Lifecycle sampling counts are 10 cold cycles, 3 fast reopens and 30 warm cycles per target, plus one pass per targeted scenario.

## 补充说明（人工记录）

- 先前尝试：evidence/lifecycle-2026-10-22T212236+0800（同一冻结代码，未启用本次的只读文档与焦点保护）。该次 C/Debian 的 cycle-015 与 D/Debian 关闭窗口时，宿主键盘事件落入了 KWin 自动聚焦的测试窗口，文档被标记为已修改（wl_keyboard.key 事件、标题出现 * 前缀、出现 Save Changes 对话框），关闭移除等待超时；原始记录保留在该目录，未改写。
- 本次为最终正式运行：文档以 0444 只读方式打开，且 transient KWin focus guard 会在测试窗口获得焦点时立即把焦点还给最近的非测试窗口，避免共享桌面上的人为键入进入测试应用；其余方法、环境、镜像与场景不变。
- 先前尝试目录：evidence/lifecycle-2026-10-22T212236+0800（保留作审计，不作为最终判定依据）。
- 正式运行前在同一环境用只读文档做过一次单窗口冒烟（启动器 1.3s 正常退出、环境 stopped、租约为空、窗口 [Read Only]），不计入正式样本。
- 第一轮（evidence/lifecycle-2026-10-22T212236+0800）的 debian13 C/cycle-015 与 D 两个 TimeoutError 经原始客户端协议日志判定为共享桌面真实键盘输入干扰，不是产品缺陷、不是合成器 close 竞态，也不是 Mousepad 崩溃/挂起：详见本目录 diagnostics/attempt1-interference.json。第一轮原始 C/D 样本未改写、未覆盖。
- 由于第一轮工具先写 SHA256SUMS、后更新 manifest.json，第一轮 SHA256SUMS 中 manifest.json 一条哈希陈旧；仅用修复后的工具重新生成第一轮的 SHA256SUMS（原始样本文件未变）。本轮最终 SHA256SUMS 已改为所有写入完成后再生成。
- 最终正式复测（本目录）在加入只读文档与焦点保护后，全场景 A–H、两台目标 0 失败：Debian/Arch 热组均 30/30、僵尸 0；Debian/Arch D 均在顶层 SIGKILL 后保留窗口并自动停止；重复首启只产生一次 start。先前第一轮的 A/B/E/F/G/H 结论与本次一致。
- 本轮运行的 tools/probe_lifecycle.py 实际哈希记录在 manifest.probe.tool_sha256；运行后发现清单写入顺序缺陷（见上条），仓库工具已修复，project/ 快照保留的仍是本轮实际执行的版本。

## 证据与复现

- 冻结源码与配置：project/（逐文件 SHA-256 与 git 状态见 manifest.json 的 snapshot）。
- 原始样本：samples/<target>/<A-H>/*/result.json、stdout.log、stderr.log、pid1-*.txt、processes-*.txt、events.jsonl。
- 机器判定：validation.json；汇总：summary.json；审计哈希：SHA256SUMS。

    python3 tools/prepare_lifecycle.py snapshot --run-dir <new-evidence-dir>
    python3 tools/prepare_lifecycle.py create --run-dir <new-evidence-dir>
    python3 tools/probe_lifecycle.py run --run-dir <new-evidence-dir>
    python3 tools/probe_lifecycle.py summary --run-dir <new-evidence-dir>

