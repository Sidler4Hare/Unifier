# Unifier


Unifier 是共享宿主内核、保留 Debian / Fedora / Arch 原生用户空间和包管理的 Linux 应用兼容项目。2026-10-23 起统一使用 **Unifier** 名称。

当前里程碑：**架构实验基线已定档**，标签 `architecture-experiment-2026-10-23`。范围、证据与下一阶段边界见[定档说明](设计/Unifier-架构实验定档-2026-10-23.md)。这是已验证实验的基线，完整文件控制层及跨宿主验收仍在后续阶段。

新入口为 `python3 tools/unifier.py`；旧 `tools/ml.py`、内部 `multilinux` 名称与现有环境身份保持兼容。两个入口共用同一实现，帮助与错误中的 `ml` 暂保留为协议兼容名称。

- [契约修复与 FreeCAD 真实 GUI 闭环](evidence/contract-freecad-gui-2026-10-23/report.md)：两处契约缺口已修复；158 项回归通过、4 跳过；宿主项目原件保存、新建对话框默认位置及停机重开通过。
- [最新改动复核与架构迭代](调研/Grok改动复核与架构迭代-2026-10-23.md)：区分代码增量、原有测试和本轮复核；该次历史基线 152 项回归通过、4 跳过，新增 13 项文件归属/恢复机制检查。
- [文件与执行控制层 v2](设计/文件与执行控制层-v2.md)：环境/数据身份、私有文件投影、保存保证、复制移动恢复和下一阶段交付关口；尚未接入产品文件层。
- [宿主权威与文件、命令、依赖边界：架构复审](设计/宿主权威与文件执行边界.md)：2026-10-23 的现状检查与下一阶段设计。升级/更换判定和 apt、dnf、pacman 驱动已经进产品；文件层与执行上下文仍是设计。
- [文件层可行性、工程难度与性能需求评估](调研/文件层可行性与性能评估-2026-10-23.md)：机制约束、28 波文件访问实验、资源预算与阶段工期估计。
- [环境文件视图原型](调研/环境文件视图原型-2026-10-23.md)：rootless 合并根对宿主进程不可见；绝对链接和独立卷已在临时 Debian 容器上核对。
- [跨应用保存原型](调研/跨应用保存原型-2026-10-23.md)：Arch FreeCAD 把共享项目写回原路径；备份、STEP、默认位置和交错写入已核对。

首版范围：**Debian 13、Fedora 44、Arch Linux 当前滚动版**，`linux/amd64`，共享宿主内核，保留各自原生包管理和运行环境。本轮在 Fedora 44 宿主准备三套用户空间。

- [第一版准备与实施](设计/第一版准备与实施.md)：范围、准备命令、接口边界和 M0–M5 实施顺序。
- [三个实验目标](config/targets.toml)与[镜像锁](config/images.lock.json)：正式实验使用平台专属 digest；Arch 的 `latest` 只用于显式刷新。

- [发行版差异与性能占用分析](调研/发行版差异与性能占用分析.md)：发行版机制比较、性能路径、内存与磁盘共享边界，以及当前机器的基线。
- [性能验证方案](调研/性能验证方案.md)：区分兼容设施开销与发行版软件差异的实验设计。
- [主机基线快照](evidence/host-baseline-2026-10-18.json)：2026-10-18 09:47:25，Asia/Shanghai。
- [基线采集脚本](tools/collect_host_baseline.py)：仅使用 Python 标准库，读取主机状态和少量工具版本。
- [文件共享实验](evidence/file-sharing-probe-2026-10-18.json)：核对同 inode、普通副本及 reflink 文件映射的 PSS。
- [文件共享实验脚本](tools/probe_file_sharing.py)：使用临时文件和短暂子进程，完成后清理。

准备与核对三套基础环境：

```bash
python3 tools/lab.py doctor --output evidence/lab-preflight-current.json
python3 tools/lab.py lock
python3 tools/lab.py pull --output evidence/lab-pull-current.json
python3 tools/lab.py smoke --output evidence/lab-smoke-current.json
```

`lock` 默认保持现有镜像身份，`lock --refresh` 才重新解析。`pull` 按顺序拉取，`smoke` 使用临时容器验证发行版版本、共享内核、UID/GID、原生包查询与相同绝对路径的文件读写。准备工具使用 Python 标准库、rootless Podman 和 Skopeo。

检查准备工具：

```bash
python3 -m unittest discover -s tests -v
```

重新采集主机基线：

```bash
python3 tools/collect_host_baseline.py --output evidence/host-baseline-current.json
```

**M0 准备已完成。** [主机预检](evidence/lab-preflight-2026-10-18.json)通过，[三套镜像](evidence/lab-pull-2026-10-18.json)已锁定并缓存，[基础探针](evidence/lab-smoke-2026-10-18.json)为 3/3 通过；6 项契约检查通过。三套原生包清单保存在 [包清单目录](evidence/lab-smoke-2026-10-18.packages/)。探针容器已清理，镜像和实验记录保留。

管理持久环境：

```bash
python3 tools/unifier.py env create --target debian13
python3 tools/unifier.py env start debian13
python3 tools/unifier.py env run debian13 --workdir /usr -- true
python3 tools/unifier.py env stop debian13
python3 tools/unifier.py env inspect debian13
python3 tools/unifier.py env remove debian13 --yes
```

`run` 要求环境已经启动。`--` 之后的参数按 argv 传入该环境，退出码是该进程的退出码。宿主环境变量默认不进入环境；要带入的名字重复使用 `--env KEY`。`--share` 把宿主上的一个绝对路径目录按相同路径挂入环境。`remove` 会删除环境的可写根，所以必须带 `--yes`。

`env plan <id>` 只读。不带其他参数时，它说明这份环境的目录契约：包内容可以原地升级；基础镜像、用户命名空间、网络、桌面、共享目录和发行版身份要另建环境，旧环境保留到明确删除。`--target`、`--network`、`--userns`、`--desktop` / `--no-desktop`、`--share` 和 `--ipc` 用来试算一次更换，不会创建或停止容器。

**M1 生命周期已完成。** 2026-10-19 15:27:10–15:28:04 +08:00，Debian 13、Fedora 44 和 Arch rolling 三个临时环境完成创建、argv、工作目录、退出码、来源隔离、停止后再次启动和共享文件属主检查，随后已删除。证据见 [生命周期验收](evidence/m1-lifecycle-2026-10-19.json)。产品代码在 `src/multilinux/`。准备工具仍然只负责镜像和基础探针。

安装、更新和卸载走各环境自己的包管理器。安装和更新要在创建时选择 `pasta`：

```bash
python3 tools/unifier.py env create --target debian13 --network pasta
python3 tools/unifier.py pkg install debian13 tree --use-command tree --use-command --version
python3 tools/unifier.py pkg query debian13 tree
python3 tools/unifier.py pkg update debian13
python3 tools/unifier.py pkg remove debian13 tree --use-command tree --use-command --version
python3 tools/unifier.py pkg log debian13
python3 tools/unifier.py pkg show TXNID
```

`env create` 的 `--network` 只能是 `none` 或 `pasta`，默认 `none`。安装、更新，以及带 `--file` 的本地包安装，需要 `pasta`；以 `none` 创建的环境会在写入事务之前被拒绝。查询和卸载使用环境里已经存在的包数据库。`--use-command` 重复出现，拼成验收 argv：安装和更新时期望退出码为 0，卸载时期望它不是 0。`--file` 与仓库包名互相排斥。成功的事务把 JSON 写到标准输出，退出码为 0；失败的事务同样写出 JSON，退出码为 1。进度写在标准错误。

**M2 原生包管理已完成。** 2026-10-19 18:40:43–18:51:08 +08:00，三个临时环境安装、使用、更新并卸载了 `tree`，另有一次记录在案的安装失败。实验包选用 `tree`，因为当天 Arch 官方仓库没有 GNU hello。宿主的 2520 个 RPM 校验值全程未变，正在操作的环境也没有改动另外两个环境的包清单。容器随后删除。证据见 [包事务验收](evidence/m2-packages-2026-10-19.json)。

现场验收会下载软件源索引，并在 Arch 上做一次整环境升级。镜像仍使用已经锁定的摘要：

```bash
python3 -m unittest discover -s tests -v
MULTILINUX_INTEGRATION=1 MULTILINUX_EVIDENCE=1 python3 -m unittest tests.test_pkg_integration -v
```

桌面接入在创建环境时打开。`--desktop` 挂上当前 Wayland、X11 和音频套接字，并让这个环境使用 `label=disable`，这样容器才能连上宿主会话，同时不去改套接字的 SELinux 标签。普通环境仍然使用默认容器标签。

```bash
python3 tools/unifier.py env create --target debian13 --id m3debian --network pasta --desktop --share /tmp/ml-m3-docs
python3 tools/unifier.py app add m3debian m3note --exec /usr/bin/gvim --workdir /tmp/ml-m3-docs --set GDK_BACKEND=x11 -- -N -n -u NONE -U NONE --servername MLNOTE --nofork
python3 tools/unifier.py app export m3note
python3 tools/unifier.py app open m3note -- /tmp/ml-m3-docs/note.mlnote
python3 tools/unifier.py app show m3note
python3 tools/unifier.py app unexport m3note
python3 tools/unifier.py app remove m3note
```

`app open` 会在环境尚未启动时先启动它，并一直保持到应用退出。导出的桌面项调用 `ml.py app open`，文件类型默认是 `application/x-multilinux-note`，扩展名是 `mlnote`。工作目录必须位于某个 `--share` 目录里。下次登录如果 `XAUTHORITY` 路径变了，需要重新创建带 `--desktop` 的环境。

对 Chromium 等要求普通用户身份的应用，创建环境时加 `--userns keep-id`，登记应用时加 `--as-host-user`。包管理和 `env run` 仍使用容器内 root；该应用的 `app open`、`app exec` 使用宿主 UID/GID。账户主目录和 `HOME` 均为容器内的 `/home/multilinux-<UID>`，共享文档仍归宿主用户。默认模式保持原有 UID 映射；已创建的环境不能直接更换映射。

**M3 桌面接入已完成。** 2026-10-19 19:26:28–19:28:27 +08:00，一个 Debian 环境安装了 `vim-gtk3`，在当前 Wayland 会话里打开 `note.mlnote`，由 gvim 自己写入 `multilinux-desktop-saved`，退出后再次打开仍读到这一行。Wayland 握手收到 3028 字节；`pactl info` 看到 `PulseAudio (on PipeWire 1.6.9)`。宿主的 2520 个 RPM 校验值未变。容器、桌面入口和文件关联随后删除。证据见 [桌面接入验收](evidence/m3-desktop-2026-10-19.json)。Fedora 和 Arch 没有重复这一个图形场景。

```bash
python3 -m unittest discover -s tests -v
MULTILINUX_INTEGRATION=1 MULTILINUX_EVIDENCE=1 python3 -m unittest tests.test_desktop_integration -v
```

## M4 资源验证

`ml.py res` 提供单次和时序采样：

```bash
python3 tools/unifier.py res sample <env>
python3 tools/unifier.py res series <env> --samples 10 --interval 1 --settle 5
```

`sample` 读取容器 cgroup 的 `memory.current`、`memory.peak`、`memory.stat`、`memory.events`、swap、pressure 和 pids，逐个进程汇总 `/proc/PID/smaps_rollup` 的 PSS，并把 conmon 单独记录。停止的环境没有 cgroup，字段保持 null 并写明原因。磁盘读取 Podman 的 `SizeRw` 与 `SizeRootFs`；`SizeRootFs` 含共享下层，不能跨环境相加。`series` 只在首尾采磁盘。当前内核接受写 `memory.peak` 但不归零，`--reset-peak` 会如实标记失败，不用 0 填补。

M4 实验驱动：

```bash
python3 tools/probe_resources.py states --output evidence/m4-states-2026-10-21.json
python3 tools/probe_resources.py same-binary --output evidence/m4-same-binary-2026-10-21.json
python3 tools/probe_resources.py update-peak --output evidence/m4-update-peak-2026-10-21.json
```

实验用锁定镜像、串行环境和隔离的临时项目状态；同二进制实验使用 freestanding 静态探针 `fixtures/resource_probe.c`，源码、二进制和 4096 字节输入的 SHA-256 都记录在证据中。更新实验在安装与更新之间停止并重启环境，使 `memory.peak` 属于新的 cgroup。

**M4 资源验证已完成有界采集。** 2026-10-21 09:32–09:45 +08:00，Fedora 44 宿主，load 1.5–2.8，内存 PSI some avg10 约为 0，CPU governor 为 powersave。三个环境均使用锁定镜像，容器和临时目录在实验后删除。

| 环境 | 空闲 PSS | 活动 PSS | 退出后 PSS | 空闲 memory.current | 活动 memory.current | 镜像逻辑大小 | 可写层 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Debian 13 | 1.56 MiB | 17.59 MiB | 1.56 MiB | 0.29 MiB | 16.43 MiB | 162.5 MB | 11.9 kB |
| Fedora 44 | 1.69 MiB | 17.72 MiB | 1.69 MiB | 0.27 MiB | 16.41 MiB | 382.1 MB | 44.6 kB |
| Arch rolling | 2.19 MiB | 18.21 MiB | 2.19 MiB | 0.48 MiB | 16.61 MiB | 410.4 MB | 11.9 kB |

活动负载是同一个静态探针：先分配并触达 16 MiB 匿名页，再占用 CPU 20 秒。三者的 PSS 增量都是 16.02 MiB，退出后回到空闲值；停止后 cgroup 消失，可写层仍在。`podman system df -v` 中三个基础镜像的 `SHARED SIZE` 都是 0 B，`UNIQUE SIZE` 等于镜像大小；这里的共享列只表示镜像层与其它镜像的共享，容器 `SizeRootFs` 仍包含同一镜像的下层，不能当作独占磁盘。

同二进制对照每个环境记录 30 个短命令样本和 7 个 2000 万次迭代样本，三种路径按轮次交替执行；表内是同轮次相对宿主 p50 的比值（三个环境的范围）：

| 执行路径 | 短命令 p50 | 配对墙钟比 | 长负载 p50 | 配对墙钟比 | 配对进程 CPU 比 |
|---|---:|---:|---:|---:|---:|
| 宿主直接执行 | 1.03–1.08 ms | 1.00 | 113–115 ms | 1.00 | 1.00 |
| `podman exec` | 160–163 ms | 153–155× | 242–247 ms | 2.12–2.16× | 0.99–1.01 |
| `ml.py env run` | 1047–1061 ms | 980–1022× | 1127–1134 ms | 9.82–9.94× | 0.99–1.01 |

每个环境 111 个样本，共 333 个；退出码、`result` 输出和 4096 字节输入哈希全部一致。短命令的墙钟差是固定入口成本；长负载中探针自己的 CPU 时间基本不变，约 2.1× 和 9.9× 的墙钟差来自容器启动、Podman 和 CLI 入口。`ml.py` 比 `podman exec` 多约 0.89 秒，这段包含 Python 启动、项目配置加载、每次 `podman info` 的 rootless 校验和环境检查；本阶段只记录，没有改动这项安全检查。

原生更新在同一低负载窗口重采，安装与更新各用一相：

| 环境 | 安装耗时 | 安装峰值 | 更新耗时 | 更新峰值（新 cgroup） | 更新内容 | 内容修订 |
|---|---:|---:|---:|---:|---|---|
| Debian 13 | 22.0 s | 163.7 MB | 13.8 s | 134.1 MB | `liblzma5` 升级 | 2 → 3 |
| Fedora 44 | 58.7 s | 422.9 MB | 11.9 s | 164.8 MB | 2 个包升级/替换 | 2 → 3 |
| Arch rolling | 73.4 s | 286.3 MB | 6.5 s | 50.1 MB | 安装阶段 `pacman -Syu` 完成整环境升级；更新阶段无事可做 | 2 → 2 |

峰值含文件缓存和内核计费，PSS 与 cgroup 内存不能相加。Arch 的安装阶段已经执行完整升级，因此它的“更新峰值”只覆盖后续同步；Debian 和 Fedora 的更新阶段执行了实际升级。可写层峰值分别为 22.9 MB、121.4 MB、121.1 MB。原生命令和验证结果都在证据里。

```bash
python3 -m unittest discover -s tests -v
MULTILINUX_INTEGRATION=1 MULTILINUX_EVIDENCE=1 python3 -m unittest tests.test_resources_integration -v
```

离线检查 53 项中 49 项通过，4 项现场测试按设计跳过；[资源采样集成测试](evidence/m4-resources-integration-2026-10-21.json) 单独通过。证据：[状态采样](evidence/m4-states-2026-10-21.json)、[同二进制](evidence/m4-same-binary-2026-10-21.json)、[更新峰值](evidence/m4-update-peak-2026-10-21.json)、[实验前宿主基线](evidence/m4-host-baseline-2026-10-21.json)。实验前 09:16 的宿主快照处于 load≈20、内存 PSI some≈25% 的窗口，因此没有采用那一窗口的正式性能数据。

上午这一轮只覆盖 Fedora 44 宿主和一次串行运行。CPU 频率、温度和网络速度没有单独控制。

### 复跑

2026-10-21 10:10:57–10:20:41 +08:00，同一台 Fedora 44 宿主、同一组锁定镜像复跑了三组实验。开始时 load 为 0.42、0.60、1.53，内存 PSI `some avg10` 为 0，CPU governor 仍是 `powersave`。采样同时记录各 CPU 的 `scaling_cur_freq` 和封装温度。状态采样期间 1 分钟负载为 0.35–1.59，封装温度 51–56°C；活动阶段最高频率约 1.8–1.9 GHz，空闲时最低约 0.4 GHz。同二进制实验自身把 1 分钟负载推到 3.93，内存 PSI `some avg10` 短时到 0.74，封装温度最高 67°C。三个环境的镜像 digest 和基础包清单哈希与上午相同。容器和临时目录在实验后删除。证据记录了采样器源码的 SHA-256；Git 仍没有提交，因此没有提交号。

三个环境的活动 PSS 增量仍是 16.023 MiB，活动样本的最小值与最大值相同，探针 CPU 时间为 19.89–19.93 秒。空闲 PSS 为 Debian 1.69 MiB、Fedora 1.56 MiB、Arch 2.25 MiB。包清单没变，空闲值相对上午移动了数十到 128 KiB，来自 `sleep` 文件页的 PSS 分摊。

同二进制再次 333 个样本，签名全部一致。三个环境的 p50 范围：

| 执行路径 | 短命令 p50 | 配对墙钟比 | 长负载 p50 | 配对墙钟比 | 配对进程 CPU 比 |
|---|---:|---:|---:|---:|---:|
| 宿主直接执行 | 1.01–1.16 ms | 1.00 | 107–126 ms | 1.00 | 1.00 |
| `podman exec` | 151–171 ms | 137–150× | 225–273 ms | 2.10–2.19× | 0.994–1.012 |
| `ml.py env run` | 985–1141 ms | 910–974× | 1045–1333 ms | 9.32–10.54× | 0.984–1.007 |

长负载的进程 CPU 时间仍与宿主直跑基本相同。`ml.py env run` 比直接 `podman exec` 多 0.83–0.97 秒。短命令比值比上午略宽，量级相同。

更新实验每相开始时的 cgroup 寿命峰值都是 1.0–1.5 MB，结束时的峰值是该相采样期间升上去的值。`memory.peak` 重置仍然失败。

| 环境 | 安装耗时 | 安装寿命峰值 | 安装 current 最大 | 更新耗时 | 更新寿命峰值 | 更新开始峰值 | 更新 current 最大 | 更新 PSS 最大 | 更新内容 | 内容修订 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| Debian 13 | 13.3 s | 185.7 MB | 183.7 MB | 9.7 s | 134.5 MB | 1.2 MB | 116.9 MB | 128.9 MB | `liblzma5` 升到 `5.8.1-1+deb13u2` | 2 → 3 |
| Fedora 44 | 29.1 s | 435.1 MB | 411.5 MB | 8.2 s | 151.9 MB | 1.5 MB | 144.1 MB | 165.5 MB | `systemd-libs`、`systemd-standalone-sysusers` 升到 `259.9-1.fc44` | 2 → 3 |
| Arch rolling | 21.4 s | 312.5 MB | 290.7 MB | 5.3 s | 48.8 MB | 1.5 MB | 42.5 MB | 53.9 MB | 安装阶段完成整环境升级；更新阶段同步后无事可做 | 2 → 2 |

Arch 更新阶段的 48.8 MB 发生在事务采样期间。上午那次采样到的 `memory.current` 最高只有 5.7 MB，寿命峰值却是 50.1 MB；这次峰值从 1.5 MB 升到 48.8 MB，采样到的 current 最高 42.5 MB。空同步也会把包数据库读进内存，0.5 秒采样可能错过短峰。Fedora 和 Arch 的更新 PSS 最大值高于 cgroup 寿命峰值，共享文件页可以进入 PSS。PSS 与 cgroup 内存仍不能相加。安装耗时比上午短，其中有软件源和缓存差异。

证据：[状态采样](evidence/m4-states-2026-10-21T101057+0800.json)、[同二进制](evidence/m4-same-binary-2026-10-21T101057+0800.json)、[更新峰值](evidence/m4-update-peak-2026-10-21T101057+0800.json)。复跑前离线检查 55 项，51 项通过，4 项现场测试按设计跳过。

两轮都没有测量 GUI 活动、完整 init、硬件加速、I/O 或跨宿主差异。频率没有锁住。M5 仍是 Debian 13、Fedora 44、Arch rolling 三类宿主上的同一组场景。

## 三环境真实图形应用实测

2026-10-22 08:11:45–08:20:23 +08:00，三套环境的 gVim 均完成原生安装、实际窗口打开、编辑保存、正常退出、整个环境停止及重新启动后的文件重开。Fedora 和 Arch 的图形文件流程在本轮补齐。

| 环境 | 实际编辑器包版本 | 安装、运行、保存、重开 |
|---|---|---|
| Debian 13 | `vim-gtk3 2:9.1.1230-2` | 通过 |
| Fedora 44 | `vim-X11 9.2.1202-1.fc44.x86_64` | 通过 |
| Arch rolling | `gvim 9.2.1178-1` | 通过 |

测试文件路径含中文和空格，内容含中文与引号。先检查编辑器缓冲区已修改而宿主文件仍未改变，再让 gVim 自己保存；重开后缓冲区、磁盘字节与 SHA-256 均一致，文件 UID/GID 为宿主用户的 `1000:1000`。六张窗口截图、包版本/清单、原生安装事务和资源快照保存在 [本轮报告](evidence/gui-apps-2026-10-22T081145+0800/report.md)与[总记录](evidence/gui-apps-2026-10-22T081145+0800/manifest.json)。

新增 [实测工具](tools/probe_desktop_apps.py)。默认创建新的证据目录、15 个源码/配置文件的独立快照以及三套环境；不会操作旧 `m3debian`。安装与图形负载串行运行，Arch 使用完整 `pacman -Syu`。流程结束后保留已安装环境和文档，并停止环境。

```bash
python3 tools/probe_desktop_apps.py run
python3 tools/probe_desktop_apps.py open --run-dir evidence/gui-apps-2026-10-22T081145+0800 --target fedora44
```

将 `--target` 换为 `debian13` 或 `arch` 可打开另外两套。它们使用证据目录中独立快照的环境记录，因此不出现在主项目的 `ml.py env list` 中。查看和停止它们使用快照里的 CLI：

```bash
python3 evidence/gui-apps-2026-10-22T081145+0800/project/tools/unifier.py env list
python3 evidence/gui-apps-2026-10-22T081145+0800/project/tools/unifier.py env stop gui-fedora44
```

宿主的 2521 个 RPM 包清单、桌面入口名单、`mimeapps.list` 和主项目原有环境/启动记录核对不变；代码回归 55 项，51 项通过，4 项现场测试默认跳过。图形测试通过 gVim 自身接口自动完成，窗口经 GTK3/X11 和当前 XWayland 会话显示。原生 Wayland GUI、人工鼠标键盘操作、剪贴板、实际音频播放、GPU 和跨宿主仍待验证。下次登录的认证路径可能变化，届时需创建新一轮环境。10 月 21 日的 M4 性能数据仍保持原来的实验范围，本轮 GUI 资源快照不作为性能排名。

## 原生 Wayland、剪贴板、音频与腾讯会议

2026-10-22 后续实测补齐了三套环境的原生 Wayland 应用流程：实际 Mousepad 窗口通过应用自身动作接收宿主中文剪贴板、复制回宿主、保存文件，并在停止整个环境后重新打开，核对缓冲区与磁盘字节、哈希及宿主文件属主。

| 环境 | 实际 Mousepad 版本 | Wayland、双向剪贴板、保存重开 | 实际播放 |
|---|---|---|---|
| Debian 13 | `0.6.3-1` | 通过 | 523 Hz，通过 |
| Fedora 44 | `0.7.0-1.fc44.x86_64` | 通过 | 659 Hz，通过 |
| Arch rolling | `0.7.0-1` | 通过 | 784 Hz，通过 |

原生后端由 KWin `XdgToplevelWindow` 与实际 Wayland buffer 日志共同验证。音频从容器内播放到当前 LinkBuds Open 蓝牙输出，宿主只采本次播放流的 PCM，核对频率和非零采样；用户回复“能听到”。原有剪贴板内容已恢复，未写入证据。Fedora/Arch 初次采样的设备绑定时序问题已修复，失败记录和单独复测均保留。

用户提供的 `TencentMeeting_0300000000_3.26.10.401_x86_64_default.publish.officialwebsite.deb` 已用原生 APT/dpkg 安装到独立 Debian 环境，并连续两次启动到完整登录界面，其中第二次是在停止整个环境后重开。修复内容包括缺失运行库、中文 locale、安装辅助工具、普通 UID/GID、账户主目录，以及独立 D-Bus 和软件渲染配置。产品新增 `--userns keep-id` 与 `--as-host-user`，对应新建环境的身份/权限现场契约通过。

腾讯会议按原始厂商启动器使用 **XWayland**。早间报告的验收边界止于登录界面；其后的摄像头黑屏、卡顿和 CPU 占用修复已由用户实测确认通过，并合入 `main`。旧失败记录保留作历史诊断，不再作为当前未完成项。修复涉及 GPU 设备与 IPC、`QT_X11_NO_MITSHM=1` 和 Mesa 硬件渲染。当前工作转入通用 ScreenCast、DMA-BUF 与 MemFd 协议验证，接手信息见 [HANDOFF.md](HANDOFF.md)。跨宿主 M5 仍未验收。

全部测试环境已保留并停止，旧环境未启动；宿主包、桌面入口、MIME 及主项目状态保持不变。62 项代码回归中 58 项通过、4 项旧现场测试默认跳过。本轮新流程由独立工具实际执行。证据见[完整报告](evidence/wayland-media-2026-10-22T082942+0800/report.md)、[机器核验](evidence/wayland-media-2026-10-22T082942+0800/validation.json)和[总记录](evidence/wayland-media-2026-10-22T082942+0800/manifest.json)。

打开后续修复并保留登录状态的腾讯会议部署：

```bash
python3 /home/omg/.local/share/multilinux/apps/wemeet/tools/unifier.py app open wemeet --
```

该启动配置使用按需生命周期。手动启动并保留的环境需用同一部署 CLI 执行 `env stop meeting`；关闭前确认其中没有正在进行的会议。新登录会话可能需要更新认证挂载。

## ScreenCast、GPU 与 CPU 协议连接

2026-10-22 后续完成独立 Debian 13 原生消费端的通用窗口采集：系统 Portal 选择合成窗口，取得受限 PipeWire FD，经现有过滤 D-Bus 代理传入容器。MemFd 与 DMA-BUF 均实际收到 60 帧 BGRx、900×598，色块及变化的帧哈希通过。不存在的 GPU 设备会明确失败，再重新取得 Portal FD、自动改用 MemFd 取得 60 帧。

GPU 路径实际完成 DMA-BUF → EGLImage → GLES/FBO → `glReadPixels`，宿主与 Debian 私有 GBM 自检各有 7747/7747 RGB 像素匹配。读回核验明确 `zero_copy=false`。当前现场只覆盖单 plane、linear modifier 与本机 Intel GPU；其它 modifier、GPU、长期预览/编码界面和跨宿主仍待验证。

代码回归 102 项，98 通过，4 项旧现场测试默认跳过；新采集由现场工具实际执行。实验环境停止、租约和代理回收，宿主包与桌面入口清单前后相同。详见[报告](evidence/screencast-2026-10-22T155700+0800/report.md)、[机器核验](evidence/screencast-2026-10-22T155700+0800/validation.json)与[接手入口](HANDOFF.md)。

## GUI 拉起与生命周期性能

2026-10-22 晚对独立 Debian/Fedora/Arch 原生 Mousepad 各测 30 次冷启动、30 次热启动。窗口加实际 Wayland buffer commit 的就绪 p50 分别为：冷启动 **0.871 / 0.969 / 0.891 秒**，热启动 **0.592 / 0.672 / 0.586 秒**。冷启动指容器和代理停止后重开，保留文件缓存；热组人为保留环境，每次启动新的应用进程。未测物理呈现，也不是腾讯会议的启动基准。

正常并发共用通过，但敏捷启停仍有三项现场问题：完整停止约 **8.36 秒**，停止期间快速重开约 **8.94–9.10 秒**；PID 1 不回收孤儿子进程，Arch 连续热组打开后发现 **155 个僵尸子进程**；强杀启动器后自动启停所有权丢失，后续正常启动也不能恢复。问题均保留证据，本轮没有修改生产启停逻辑。

180 个正式样本和 15 个生命周期场景执行完成，另保留一次已修正的测量探针时序失败。105 项回归中 101 通过、4 跳过；机器证据有效，产品结论为 **需要修复生命周期**。测试环境、窗口、代理和临时 KWin 脚本已回收，原部署保持原状态。详见[完整报告](evidence/startup-2026-10-22T182400+0800/report.md)、[机器核验](evidence/startup-2026-10-22T182400+0800/validation.json)和[接手入口](HANDOFF.md)。

## 复杂 Arch 应用（2026-10-22 晚）

[本轮报告](evidence/arch-complex-2026-10-22T202359+0800/report.md)：FreeCAD 原生 Wayland 的建模、参数重算、中文 FCStd/STEP 保存与回读、停机重开通过；GitHub Release Sunshine 原生包安装与命令入口通过，但 udev/内核模块/systemd 钩子失败，系统集成与串流未验收。发现 pacman 返回 0 时项目 `completed` 不体现这些钩子错误。独立实验环境已停止并保留；生产生命周期缺陷已在本轮后续修复，见下节。[后续测试矩阵](调研/复杂Arch应用与后续测试.md)。


## 生命周期与安装状态修复（2026-10-22 深夜）

用户要求先完成生命周期缺陷和安装状态准确性修复与验证，再做并发测试。本轮修改了生产代码 `src/multilinux/`，工作树尚未提交；产品行为由证据目录 `project/` 中的冻结快照复现。

### 生命周期

三项旧现场缺陷都已在代码中修复：

- 容器创建参数加入 `--init`，PID 1 为 catatonit：SIGTERM 可转发给 sleeper，孤儿进程由 PID 1 回收。旧容器的 `sleep` PID 1 不再作为基线。
- 受管应用启动改为“短启动器 + 脱离会话的 worker”：worker 持有租约并在应用退出时释放；启动器被 SIGKILL 后窗口关闭仍能自动停止环境与桥接。worker 用环境标记防递归，退出码按 shell 约定映射。
- 租约所有权按 `container_id` + `container_started_at` 继承；手动 stop/start 会改变 StartedAt，从而不被误停。最后租约释放始终保存租约移除并清理桥接；stop 失败时保留 `auto_stop` 供下一次受管启动重试。
- `start`/`stop` 增加按环境的跨进程 flock，并等待 podman 的 `stopping` 状态结束（上限 15 秒）；锁序固定为 lease lock → lifecycle lock。桥接按“进程存活 + socket 仍是 socket”复用，失效则重建。
- 代码位置：`src/multilinux/runtime.py`、`on_demand.py`、`service.py`、`cli.py`；离线契约测试 `tests/test_lifecycle_contracts.py`（26 项）。

真机复验使用两个独立环境（Debian 13、Arch）和原生 Mousepad，窗口就绪仍采用 KWin 原生 toplevel + 非空 buffer commit：

| 场景 | 第二轮结果 |
|---|---|
| 冷启动 10 次/环境 | 20/20 通过；就绪 p50 Debian 884 ms、Arch 887 ms |
| 关窗→容器 died 事件 | p50 199 ms（旧 8.36 s 级停止） |
| 关窗→环境记录 stopped | p50 24.5 ms；20/20 容器退出码 143，PID 1 均为 podman-init |
| 关窗后 150–250 ms 快速重开 | 6/6 通过；重开就绪 p50 884 ms（旧约 8.9 s），单容器 |
| 30 次热组开关后僵尸 | 两环境均 0 僵尸；PID 1 直接子进程 1 |
| 只 SIGKILL 顶层启动器 | 两环境窗口存活；关窗后自动停止；随后正常循环恢复 auto_stop |
| 同时 SIGKILL 启动器与 worker | 两环境按可接受路径处理；后续正常受管循环最终自动停止 |
| 双窗口并发租约 | 两环境单容器、lease=2；关一个仍 running，关最后一个自动停止 |
| 手动预启动保留 | 两环境受管关窗后仍 running |
| 停止态 3 次并发首启 | 两环境均 0 次 create、1 次 start、单容器；全部关闭后自动停止 |

第二轮 `product_verdict=lifecycle-fixed`，残留窗口/进程/租约/桥接均为 0。第一轮在同样的冻结代码下出现 2 次 Debian 关窗测量超时（C/cycle-015 与 D）：协议日志显示共享桌面的真实键盘输入把文档标记为已修改，Mousepad 弹出自身 Save Changes 模态框，不是合成器或产品停止流程缺陷；原始失败证据保留为 `lifecycle-2026-10-22T212236+0800`。第二轮改用只读文档并加临时 KWin 焦点保护后完整重跑 A–H 全部通过，最终判定以第二轮为准。

证据：[第二轮报告](evidence/lifecycle-2026-10-22T213032+0800/report.md)、[第二轮机器核验](evidence/lifecycle-2026-10-22T213032+0800/validation.json)、[第一轮保留失败](evidence/lifecycle-2026-10-22T212236+0800/report.md)。复现入口：

```bash
python3 tools/prepare_lifecycle.py snapshot --run-dir <new-evidence>
python3 tools/prepare_lifecycle.py create --run-dir <new-evidence>
python3 tools/probe_lifecycle.py run --run-dir <new-evidence>
python3 tools/probe_lifecycle.py summary --run-dir <new-evidence>
```

### 安装状态准确性

事务新增分层字段，避免把“pacman 退出 0”当成完整成功：

- `maintenance = {status: ok|warnings, items: [...], system_integration: "unverified", full_init: false}`；
- `verdict = verified | verified-with-warnings | failed`；`phase` 仍只表示包事务是否完成，包已安装不会因维护钩子失败被判 failed；
- CLI 对 `verified-with-warnings` 在 stderr 打印一行警告并仍返回 0；`pkg log` 增加 verdict/maintenance 字段；
- 扫描完整 stdout/stderr 识别 pacman 钩子失败、rpm/dnf scriptlet 失败、`modprobe: FATAL`、systemd 非 PID 1、systemd system bus 不可用、udev uevent 写入失败；普通容器条件（`Skipped: Current root is not booted.`、`ignoring`）和普通 `error:` 不误报。

真实 Arch 环境（从已停止的 complex-arch 证据容器提交本地镜像，新建干净环境）3 个事务：

| 用例 | phase | verdict | maintenance |
|---|---|---|---|
| ml-benign + `/bin/true` | completed | verified | ok |
| ml-hooktest（故意失败的 alpm 钩子，pacman 退出 0） | completed | verified-with-warnings | pacman-hook-failed ×1 |
| 真实 Sunshine 本地包 + `sunshine --version` | completed | verified-with-warnings | udev ×43、modprobe ×3、systemd-not-pid1 ×1、systemd-bus ×1、pacman-hook-failed ×2 |

三个用例的包都在清单中、use command 退出 0，宿主 RPM 清单与桌面入口前后不变。Sunshine 仍只证明“包已安装、命令入口可运行、维护钩子失败可见”，不证明 udev/uinput/systemd 或串流可用。

证据：[安装状态报告](evidence/install-status-2026-10-22T2108+0800/report.md)、[机器核验](evidence/install-status-2026-10-22T2108+0800/validation.json)。新增工具 `tools/prepare_install_status.py`、`tools/probe_install_status.py`。

### 回归与边界

- 全量离线回归 **149 项通过**（4 项既有 `MULTILINUX_INTEGRATION` 现场测试按设计跳过）。
- 生命周期真机只覆盖 Fedora 44 宿主上的 Debian 13 与 Arch；Fedora 环境未在本轮重复。安装状态真实站只覆盖 Arch；Debian/Fedora 文本规则仅有离线测试。
- 生命周期第二轮开始时宿主可用内存最低约 2.43 GiB、内存 PSI some avg10 max 0；未锁频，仍是日常使用宿主上的工程测量。
- 本轮实验容器全部停止保留，运行中容器只剩用户原有的腾讯会议部署；无存活租约、桥接或测试窗口。

## 并发测试：共享环境、母发行版与双发行版、性能、敏捷启停

2026-10-22 晚，按用户确认的四项并发矩阵执行完毕：同一发行版多进程共用一套环境、系统母发行版（宿主 Fedora 44 原生 Konsole）+ Debian 13 + Arch 两套发行版的多个应用进程、共享/每进程一套环境的性能对照，以及并发下的敏捷启动关闭。证据：[并发报告](evidence/concurrency-2026-10-22T220318+0800/report.md)、[机器核验](evidence/concurrency-2026-10-22T220318+0800/validation.json)。

**判定：`concurrency-verified`。** 44/44 个设计波次通过，0 失败；`measurement_valid=true`。五个自建环境全部停止保留，无运行中容器、租约、桥接、测试窗口或实验进程残留。

### 拓扑验收：多个应用进程共用一套环境

- 五个环境：`conc-debian`、`conc-arch` 与 3 个 Debian 对照环境，均由本地 `startup-*` 镜像摘要创建，未刷新/下载；每个环境一个 on-demand Mousepad。
- **S1 共享拓扑**（Debian，3 窗口并发，5 冷 + 2 热波次）：每波容器数 **1**、容器 ID 与 prepare 记录一致、create 事件 **0**、冷启动 start 事件 **1**、live lease **3**、容器内 Mousepad 进程 **3**；关 1 个后仍 running（lease=2），关最后一个后 stopped；热追加 2 窗口额外 start 事件 **0**，lease=3。
- **S2 共享规模**：N=1/2/3/4 各 3 波，全部 12/12 通过，均单容器、单 start、N lease。
- **S3 双发行版**：Debian 3 + Arch 3 同时，3/3 通过；运行中项目容器恒为 **2**，每环境 1 容器/3 lease/3 进程。
- **S4 母发行版原生**：3 个 `konsole --separate` 窗口，3/3 通过；3 个独立宿主 PID，项目容器集合与环境记录不变，无 lease、无容器创建。
- **S5 混合**：原生 2 + Debian 2 + Arch 2，2/2 通过；运行中项目容器恒为 2，native 独立 PID。
- 整个测试期间 prepare 完成后 **create 事件 = 0**：没有任何进程按“每应用一套环境”新建发行版；同环境热追加也不产生新的 start 事件。

### 性能（描述性 p50/p95，不是应用排名）

S2 共享环境规模（每 N 3 wave，冷启动）：

| N | 通过 | 单窗口就绪 p50/p95 (ms) | 全窗口就绪 p50/p95 (ms) | 停止 p50/p95 (ms) | cgroup current p50 | cgroup peak p50 | PSS p50 | SizeRw p50 |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | 3/3 | 970.2/977.0 | 970.2/977.0 | 212.6/236.4 | 24.1 MiB | 29.2 MiB | 1802.0 kB | 127.7 KiB |
| 2 | 3/3 | 1032.7/1345.4 | 1308.8/1345.4 | 212.7/247.9 | 46.7 MiB | 51.9 MiB | 1557.0 kB | 133.2 KiB |
| 3 | 3/3 | 1350.9/1750.2 | 1617.6/1750.2 | 215.5/232.5 | 69.9 MiB | 74.8 MiB | 1454.0 kB | 141.0 KiB |
| 4 | 3/3 | 1417.4/2172.4 | 2036.0/2172.4 | 214.9/241.4 | 94.8 MiB | 99.3 MiB | 1370.0 kB | 151.2 KiB |

- 同一发行版的多个进程共享一套环境：容器启动成本只付一次，N 个窗口的全窗口就绪时间从 970 ms（N=1）增至 2036 ms（N=4），而停止时间稳定在约 215–241 ms。
- **S6 每进程一套环境对照**（3 个 Debian 环境各 1 窗口）：3 波全部通过；wave ready p50 1283.7 ms，停止 p50 202.7 ms。与共享 1 环境 N=3 相比，3 套环境的 ready 为 0.79x（启动略快，因为 3 个容器并行启动），但 cgroup current 1.20x、peak 1.35x、PSS 1.88x、SizeRw 2.36x；额外产生 3 个容器、3 个桥接和 3 份可写层。该对照只用于回答问题，不是产品默认路径。
- **S3 双发行版**：6 窗口全就绪 p50 2525.6 ms，停止 p50 246.0 ms；**S5 混合 6 窗口**（2 原生 + 4 容器）全就绪 p50 1623.3 ms。
- **S7 敏捷并发**：10 个波次 × 3 并发首启全部通过；单窗口就绪 p50 1349.7 ms，全窗口 p50 1597.7 ms；停止 p50 213.2 ms、p95 261.4 ms；停止中重开发起 150.1/200.1/250.1 ms，重开就绪 p50 1942.4 ms、p95 2056.7 ms（参考阈值 stop p95 < 1500 ms、reopen p95 < 3000 ms）；每次重开均单容器、容器 ID 恒定、3 lease、create=0、start=2（stop+restart 各一次）。
- **母发行版原生**：3 个 Konsole 窗口就绪 p50 531.1 ms、p95 574.4 ms，关闭到窗口移除 p50 22.3 ms；宿主 Konsole 进程树 PSS p50 约 47.6 MiB。
- 宿主条件：可用内存最低 1880.5 MiB，内存 PSI some avg10 max 4.69%、full max 4.59%，1 分钟 load max 3.89；未锁频，是日常使用宿主上的工程测量。

### 保留失败与口径修正

- **S5 第一轮**：原生 Konsole 在混合并发下 KWin `bufferGeometry=0`，旧就绪口径强制 bufferWidth/Height>0 导致 25 s 超时（2/2 波失败），同时 5 ms 轮询生成 3300+ 临时 KWin 脚本。改为 `windowAdded + 非空 frameGeometry`（100 ms 查询）后重跑 S5 2/2、S4 3/3 通过；原始失败保留在证据 `attempt1/S5`，不是产品拓扑/生命周期缺陷。
- **S7 第一轮**：波次全部通过，但“停止中重开”在等待启动器退出后才计时，实际发起 349.6–402.2 ms，未覆盖停止窗口；改为最后一个 close 请求后直接计时，重跑后发起 150–250 ms、10/10 + 3/3 通过。原始记录保留在 `attempt1/S7`。
- 旧 bufferGeometry 口径下的中间通过记录保留在 `attempt2/S4-buffergeom`、`attempt2/S5-buffergeom`；最终 `wave_counts` 只统计修正后样本，`retained_attempts` 明确记录审计材料，不把修正前的记录混入通过率。

### 边界

- 母发行版侧是 Fedora 44 原生 KDE Konsole，容器侧是 Debian/Arch 原生 Mousepad；软件栈和工具包不同，数字不是应用排名。
- 容器就绪是 KWin windowAdded + 非空 Wayland buffer commit；原生 Konsole 是 windowAdded + 非空 frameGeometry（未开启宿主 WAYLAND_DEBUG），不是物理呈现。
- 容器内存为 cgroup current/peak 加容器内 PSS；原生内存为宿主 Konsole 进程树 PSS；两种口径不能相加为总量。
- 本轮只覆盖 Debian 13 与 Arch；Fedora 容器并发、更大规模/长时间压力、M5 跨宿主仍不在本轮结论内。
