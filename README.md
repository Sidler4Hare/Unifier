# 多发行版 Linux 应用兼容子系统

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
python3 tools/ml.py env create --target debian13
python3 tools/ml.py env start debian13
python3 tools/ml.py env run debian13 --workdir /usr -- true
python3 tools/ml.py env stop debian13
python3 tools/ml.py env inspect debian13
python3 tools/ml.py env remove debian13 --yes
```

`run` 要求环境已经启动。`--` 之后的参数按 argv 传入该环境，退出码是该进程的退出码。宿主环境变量默认不进入环境；要带入的名字重复使用 `--env KEY`。`--share` 把宿主上的一个绝对路径目录按相同路径挂入环境。`remove` 会删除环境的可写根，所以必须带 `--yes`。

**M1 生命周期已完成。** 2026-10-19 15:27:10–15:28:04 +08:00，Debian 13、Fedora 44 和 Arch rolling 三个临时环境完成创建、argv、工作目录、退出码、来源隔离、停止后再次启动和共享文件属主检查，随后已删除。证据见 [生命周期验收](evidence/m1-lifecycle-2026-10-19.json)。产品代码在 `src/multilinux/`。准备工具仍然只负责镜像和基础探针。

安装、更新和卸载走各环境自己的包管理器。安装和更新要在创建时选择 `pasta`：

```bash
python3 tools/ml.py env create --target debian13 --network pasta
python3 tools/ml.py pkg install debian13 tree --use-command tree --use-command --version
python3 tools/ml.py pkg query debian13 tree
python3 tools/ml.py pkg update debian13
python3 tools/ml.py pkg remove debian13 tree --use-command tree --use-command --version
python3 tools/ml.py pkg log debian13
python3 tools/ml.py pkg show TXNID
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
python3 tools/ml.py env create --target debian13 --id m3debian --network pasta --desktop --share /tmp/ml-m3-docs
python3 tools/ml.py app add m3debian m3note --exec /usr/bin/gvim --workdir /tmp/ml-m3-docs --set GDK_BACKEND=x11 -- -N -n -u NONE -U NONE --servername MLNOTE --nofork
python3 tools/ml.py app export m3note
python3 tools/ml.py app open m3note -- /tmp/ml-m3-docs/note.mlnote
python3 tools/ml.py app show m3note
python3 tools/ml.py app unexport m3note
python3 tools/ml.py app remove m3note
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
python3 tools/ml.py res sample <env>
python3 tools/ml.py res series <env> --samples 10 --interval 1 --settle 5
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
python3 evidence/gui-apps-2026-10-22T081145+0800/project/tools/ml.py env list
python3 evidence/gui-apps-2026-10-22T081145+0800/project/tools/ml.py env stop gui-fedora44
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
python3 /home/omg/.local/share/multilinux/apps/wemeet/tools/ml.py app open wemeet --
```

该启动配置使用按需生命周期。手动启动并保留的环境需用同一部署 CLI 执行 `env stop meeting`；关闭前确认其中没有正在进行的会议。新登录会话可能需要更新认证挂载。

## ScreenCast、GPU 与 CPU 协议连接

2026-10-22 后续完成独立 Debian 13 原生消费端的通用窗口采集：系统 Portal 选择合成窗口，取得受限 PipeWire FD，经现有过滤 D-Bus 代理传入容器。MemFd 与 DMA-BUF 均实际收到 60 帧 BGRx、900×598，色块及变化的帧哈希通过。不存在的 GPU 设备会明确失败，再重新取得 Portal FD、自动改用 MemFd 取得 60 帧。

GPU 路径实际完成 DMA-BUF → EGLImage → GLES/FBO → `glReadPixels`，宿主与 Debian 私有 GBM 自检各有 7747/7747 RGB 像素匹配。读回核验明确 `zero_copy=false`。当前现场只覆盖单 plane、linear modifier 与本机 Intel GPU；其它 modifier、GPU、长期预览/编码界面和跨宿主仍待验证。

代码回归 102 项，98 通过，4 项旧现场测试默认跳过；新采集由现场工具实际执行。实验环境停止、租约和代理回收，宿主包与桌面入口清单前后相同。详见[报告](evidence/screencast-2026-10-22T155700+0800/report.md)、[机器核验](evidence/screencast-2026-10-22T155700+0800/validation.json)与[接手入口](HANDOFF.md)。
