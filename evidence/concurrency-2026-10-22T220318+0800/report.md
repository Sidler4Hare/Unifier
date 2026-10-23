# 并发测试：共享环境、双发行版 + 母发行版、性能与敏捷启停

运行目录：concurrency-2026-10-22T220318+0800，生成时间 2026-10-22T22:19:45+08:00。宿主 Fedora 44 KDE / KWin 6.7.5，rootless Podman，基线提交 fd0b2da877ec6a4059b0260c8aa53695851f1fac（分支 codex/screencast-protocol，工作树脏）。本轮只新增测量工具与新证据目录；产品代码由 project/ 冻结快照决定，未改写已有证据。

**判定：concurrency-verified，并发拓扑、资源与敏捷启停在该场景集上成立。**

机器核验：measurement_valid=true，product_verdict=concurrency-verified。判定标准见 validation.json。

## 方法与矩阵

五个独立环境：conc-debian（Debian 13）、conc-arch（Arch）、conc-sep-1/2/3（Debian，每进程一套环境的对照）。均 --network none --desktop --portals --userns keep-id，各注册一个 on-demand Mousepad（as_host_user、GDK_BACKEND=wayland、WAYLAND_DEBUG=client、LC_ALL=C.UTF-8）。镜像来自上一轮 startup-* 本地摘要，未刷新、未下载。
母发行版侧为宿主 Fedora 44 原生 Konsole：每个窗口独立进程 konsole --separate -p tabtitle=<marker> -e sleep 300，关闭即 kill 该进程；不安装宿主包。
- 就绪口径按软件栈分别定义并写入 validation 限制：容器 Mousepad 为 KWin windowAdded + 客户端非空 Wayland buffer commit；宿主原生 Konsole 为 KWin windowAdded + 非空 frameGeometry（混合并发下其 bufferWidth/Height 可能为 0，因此不把 buffer geometry 当作原生就绪前提；KWin 几何查询间隔 100 ms，避免生成大量临时脚本）。

| 场景 | 设计 | waves | 判定 |
|---|---|---:|---|
| S1 共享拓扑 | 1 个 Debian 环境并发 3 窗口；另 2 wave 热追加 2 窗口 | 5+2 | 5/5 + 2/2 |
| S2 共享规模 | Debian N=1/2/3/4，各 3 wave | 12 | N1:3/3, N2:3/3, N3:3/3, N4:3/3 |
| S3 双发行版 | Debian 3 + Arch 3 同时 | 3 | 3/3 |
| S4 母发行版原生 | 3 个 Konsole 窗口并发 | 3 | 3/3 |
| S5 混合 | 原生 2 + Debian 2 + Arch 2 | 2 | 2/2 |
| S6 每进程一套环境 | conc-sep-1/2/3 各 1 窗口 | 3 | 3/3 |
| S7 敏捷并发 | 10 wave × 3 并发首启 + 3 次停止中重开 + 终检 | 10+3 | 10/10 + 3/3 |

波次判定（validation.all_designed_waves_passed）：S1 7/7；S2 12/12；S3 3/3；S4 3/3；S5 2/2；S6 3/3；S7 14/14。失败总数 0。

## S1 共享环境拓扑（Debian，3 窗口并发）

- 冷波次 5/5 通过：每 wave 容器数 1、容器 ID 与 prepare 记录一致、create 事件 0、start 事件 1、live lease 3、容器内 Mousepad 进程 3。
- 关 1 个后环境仍 running、lease=2 的 wave 数：5/5；关最后一个后 stopped、lease=0。
- wave ready p50/p95：1458.2/2070.5 ms；关最后一个到容器停止 p50/p95：194.4/219.5 ms。
- 热追加 2 窗口 2/2 通过：额外 start 事件 [0, 0]，lease=3，单容器 ID 恒定。
- S1 warm 的设计目标是验证“已运行环境追加窗口不新增 start 事件”，其增量窗口就绪 p50/p95 为 690.0/910.8 ms（首窗口 893.6 ms）；这些数字不作为冷启动性能样本，也不单独构成性能阈值。

## S2 共享规模 N=1..4（每 N 3 wave，cold）

| N | 通过 | 单窗口就绪 p50/p95 | wave 就绪 p50/p95 | 停止 p50/p95 | cgroup memory.current p50 | peak p50 | PSS p50 | SizeRw p50 |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | 3/3 | 970.2/977.0 | 970.2/977.0 | 212.6/236.4 | 24.1 MiB | 29.2 MiB | 1802.0 kB | 127.7 KiB |
| 2 | 3/3 | 1032.7/1345.4 | 1308.8/1345.4 | 212.7/247.9 | 46.7 MiB | 51.9 MiB | 1557.0 kB | 133.2 KiB |
| 3 | 3/3 | 1350.9/1750.2 | 1617.6/1750.2 | 215.5/232.5 | 69.9 MiB | 74.8 MiB | 1454.0 kB | 141.0 KiB |
| 4 | 3/3 | 1417.4/2172.4 | 2036.0/2172.4 | 214.9/241.4 | 94.8 MiB | 99.3 MiB | 1370.0 kB | 151.2 KiB |

## S3 双发行版并发（Debian 3 + Arch 3）

- 通过 3/3；wave ready p50/p95 2525.6/2590.4 ms；停止 p50/p95 246.0/287.5 ms。
- 运行中项目容器数（每 wave）：[2, 2, 2]；每个环境 1 个容器、3 个 lease、3 个容器内 Mousepad 进程；create 事件 0。

## S4 母发行版原生（3 个 Konsole 窗口）

- 通过 3/3；native 就绪 p50/p95 531.1/574.4 ms；关闭到窗口移除 p50/p95 22.3/22.5 ms。
- 3 个独立宿主 PID：[True, True, True]；项目容器集合与环境记录前后不变：[True, True, True]；未产生 lease。
- 宿主 Konsole 进程树 PSS p50/p95：47636.0/52350.0 kB。

## S5 混合（原生 2 + Debian 2 + Arch 2）

- 通过 2/2；wave ready p50/p95 1623.3/1768.5 ms；运行中项目容器数：[2, 2]；native 独立 PID：[True, True]。

## S6 每进程一套环境对照（3 个 Debian 环境各 1 窗口）

- 通过 3/3；wave ready p50/p95 1283.7/1417.8 ms；停止 p50/p95 202.7/203.7 ms。

| 指标（描述性） | 共享 1 环境 N=3 | 每进程 3 环境 | 3 环境 / 共享 |
|---|---:|---:|---:|
| wave 就绪 p50 (ms) | 1617.6 | 1283.7 | 0.79x |
| cgroup memory.current p50 (MiB) | 69.9 | 71.1 | 1.20x |
| cgroup memory.peak p50 (MiB) | 74.8 | 87.9 | 1.35x |
| PSS p50 (kB) | 1454.0 | 2716.0 | 1.88x |
| SizeRw p50 (KiB) | 141.0 | 340.0 | 2.36x |

- 该对照只用于回答“每进程一套环境”与共享环境的资源/启动差异，不是产品默认路径；3 套环境同时启动会额外产生 3 个容器、3 个桥接与 3 份 writable layer。

## S7 敏捷并发（3 个并发首启 ×10 + 停止中重开 ×3）

- 10 个并发首启 wave 通过 10/10；单窗口就绪 p50/p95 1349.7/1795.6 ms；停止 p50/p95 213.2/261.4 ms（参考阈值 stop p95 < 1500 ms）。
- 停止中重开 3 个 wave 通过 3/3；重开就绪 p50/p95 1942.4/2056.7 ms（参考 reopen p95 < 3000 ms）；重开发起时间 {'count': 3, 'min': 150.096, 'p50': 200.081, 'p95': 250.086, 'max': 250.086, 'mean': 200.088}。
- 每次重开均核对单容器/容器 ID 恒定/3 lease；最终手动进程表僵尸检查：[{'zombie_count': 0, 'lease_before_stop': 0, 'passed': True}]。

## 宿主条件

- 可用内存最低 1880.5 MiB；内存 PSI some avg10 最大 4.69%，full avg10 最大 4.59%；1 分钟 load 最大 3.89；宿主快照 43 个。
- 五个环境创建耗时（秒）：{'conc-debian': 1.32, 'conc-arch': 1.191, 'conc-sep-1': 1.167, 'conc-sep-2': 1.16, 'conc-sep-3': 1.16}。

## 失败项

无。所有预设判定项通过。

## 复测与保留失败

- S5：第一轮两波均失败（not every mixed window became ready）。保留目录 attempt1/S5；原因：混合并发下原生 Konsole 的 KWin bufferGeometry 为 0；原生就绪口径强制 bufferWidth/Height>0 导致 25s 超时，且 5ms 一次的几何查询在单 wave 内留下 3300+ 个临时 KWin 脚本。；影响：失败仅出现在原生侧就绪判定；拓扑检查未发现异常。原始 samples 保留在 attempt1/S5/scenario.json 与 wave-*/result.json。；复测 samples/S5：原生就绪改为 KWin windowAdded + 非空 frameGeometry、查询间隔放宽到 100ms，并修复原生 marker 跨场景复用问题后重跑 2/2 通过；native 就绪约 429–545ms，混合 6 窗口全部 ready，运行中项目容器=2，每环境 lease=2，关闭后两环境自动停止。。这是测量工具/判定口径缺陷，不是产品并发拓扑或生命周期缺陷。
- S5：中间轮次（marker 唯一化修复后）在旧 bufferGeometry 口径下通过。保留目录 attempt2/S5-buffergeom；原因：仍使用 bufferWidth/Height>0 的原生就绪口径；为保证最终证据全部使用新口径而重跑。；影响：保留作口径变更前的审计记录，不计入最终 wave_counts。；复测 samples/S5：最终 samples/S5 使用 frameGeometry 口径并通过 2/2。。这是测量工具/判定口径缺陷，不是产品并发拓扑或生命周期缺陷。
- S4：旧 bufferGeometry 口径下的通过记录。保留目录 attempt2/S4-buffergeom；原因：与 S5 同步改写原生就绪口径后重跑，保证 S4/S5 口径一致。；影响：保留作口径变更前的审计记录，不计入最终 wave_counts。；复测 samples/S4：最终 samples/S4 使用 frameGeometry 口径并通过 3/3。。这是测量工具/判定口径缺陷，不是产品并发拓扑或生命周期缺陷。
- S7：第一轮 10/10 + 3/3 通过，但“停止中重开”计时口径有缺陷。保留目录 attempt1/S7；原因：重开 wave 在关闭最后一个窗口后先等待全部启动器退出（此时环境已经停止）才计时，实际重开发起为 349.6–402.2ms，不满足要求的 150–250ms。；影响：波次本身没有失败，但并没有真正覆盖 env stopping 期间的重开；属于测量口径缺陷，原始样本保留在 attempt1/S7/。；复测 samples/S7：改为最后一个 close 请求后直接计时：重开发起 150.1/200.1/250.1ms，重开就绪 p50 1942ms，10/10 并发首启 + 3/3 停止中重开通过，create 事件 0、start 事件 2（stop+restart 各一次）、容器 ID 恒定。。这是测量工具/判定口径缺陷，不是产品并发拓扑或生命周期缺陷。

最终 validation 的 wave_counts 均为修正后正式样本的计数；attempt1/、attempt2/ 下的记录是修正前保留的审计材料，不被覆盖也不计入最终通过率。

## 清理与残留核对

- all_clean=true：运行中容器 0，存活租约 0，桥接进程 0，遗留窗口 0，遗留 ml.py/worker 0，遗留实验 Konsole 进程 0。
- 宿主包清单哈希前后一致：True；桌面入口清单哈希前后一致：True。
- 五个自建环境均停止并保留以便复现；未触碰既有 m3debian/wemeet/startup-*/gui-*/complex-arch 等环境。

## 限制

- The native side is KDE Konsole on Fedora 44; the container side is native Mousepad in Debian 13 / Arch. Different software stacks and toolkits: the numbers are not an application ranking and only describe this host.
- Readiness for containers is KWin windowAdded + non-empty Wayland buffer commit; for native Konsole it is windowAdded + non-empty buffer geometry because host WAYLAND_DEBUG logging is not enabled. Physical display presentation is not measured.
- Container memory uses cgroup memory.current/memory.peak plus in-container PSS; native memory uses host PSS of the Konsole process tree. PSS and cgroup accounting are different views and are not summed together.
- The host is a shared daily-use Fedora 44 KDE machine with no frequency locking; absolute timings are descriptive (p50/p95), not an application benchmark.
- Only Debian 13 and Arch were exercised; Fedora container concurrency and cross-host (M5) behaviour are not claimed.

## 证据与复现

- 冻结源码与配置：project/（逐文件 SHA-256 与 git 状态见 manifest.json 的 snapshot）。
- 原始样本：samples/S1..S7/<wave>/{result.json,window-*,native-*,events.jsonl,metrics-*.json,...}。
- 机器判定：validation.json；汇总：summary.json；残留核对：postconditions.json；审计哈希：SHA256SUMS。

    python3 tools/prepare_concurrency.py snapshot --run-dir <new-evidence-dir>
    python3 tools/prepare_concurrency.py create --run-dir <new-evidence-dir>
    python3 tools/probe_concurrency.py run --run-dir <new-evidence-dir>
    python3 tools/probe_concurrency.py summary --run-dir <new-evidence-dir>

