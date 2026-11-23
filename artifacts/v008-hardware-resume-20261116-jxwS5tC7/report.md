# v0.0.8 硬件续测与过渡大修

2026-11-16，Asia/Shanghai。Tailscale 和原内网尝试一开始均超时。用户切到内网后，经 `unifier-server` 登录成功，核对原 AMD Fedora 44 实例与 Radeon HD 7470（`1002:6778`、PCI `0000:01:00.0`、`renderD128`）。本轮沿用现有专用实例。

| 用例 | 结果 |
|---|---|
| 本机 i5-1135G7 / Debian 13 C04、C05 | **PASS**。客体调度、proc、宿主有效 cpuset 均为 CPU 0–7、NUMA 0。两个分别固定到 CPU 0 和 1 的进程均成功退出。有效配额为每 100 ms 分配 100 ms，聚合 scope 观察到 14 次节流。[结果](cpu-stage-disk/result.json) |
| 服务器 Xeon / Fedora 44 C04、C05 | **PASS**。客体与宿主 cpuset 均为 CPU 0–35、NUMA 0。同样的一核配额下，客体 scope 观察到 5 次节流，两个进程成功退出。[结果](server-cpu/result.json) |
| HD 7470 VA-API 能力 | **PASS**。实际驱动为 Mesa Gallium 26.2.6 `r600`（AMD CAICOS），VA-API 1.23 暴露 MPEG-2 Simple/Main VLD 和 VideoProc。当前驱动配置没有枚举 H.264 VLD 或编码入口。[结果](server-va-fixed/result.json) · [Fedora 44 libva 包](https://packages.fedoraproject.org/pkgs/libva/libva/fedora-44.html) |
| HD 7470 G07 MPEG-2 硬解 | **PARTIAL**。128×96 块状 I 帧的 18,432 个 NV12 样本与软件参考逐字节一致。另一个复杂 I 帧平均绝对误差 0.873、最大差 12，超过预设 0.35/8；复测读回一致，直接 derive 未用于本次比对。实机均经 r600 提交，复测只在目标 Radeon 上观察到 UVD fence 从 5 增至 11，且已 signal。[首次输出](server-mpeg2/result.json) · [复核](server-mpeg2-derive/result.json) · [计数前](amd-derive-ring-before.txt) · [计数后](amd-derive-ring-after.txt) · [Linux Radeon UVD ring](https://kernel.googlesource.com/pub/scm/linux/kernel/git/cel/linux/+/refs/heads/nfsd-next/drivers/gpu/drm/radeon/radeon.h) |

VA 测试只把已签名 Fedora 44 原包中的两个库作为临时探针输入，未写原生软件包数据库。MPEG-2 探针直接调用 VA-API，软件参考独立使用 FFmpeg。GPU 产物、块状与复杂样本、参考、编译回执及守卫回执均保留。

测试守卫修复了阶段磁盘统计。旧实现从多轮前整个文件系统的空闲量计差额，启动前误拒绝本轮。新实现保留原文件系统身份记录，另记每阶段空闲基准；同一 1 GiB 阶段写入上限和磁盘保留线有效。守卫 50 项定向回归通过，原拒绝回执保留。服务器首次探针因新源码快照没有共享作业锁而未启动；新快照链接到已存在的同 inode 作业锁后才在独立回执目录续跑。

本轮所有实机 worker 均退出，目标实例为 stopped/PID 0，两张 V100 专用实例仍为 stopped/PID 0。AMD 单节点 ACL 逐阶段恢复，最终原 SHA-256 一致且看门狗 MainPID=0。Git HEAD、索引和日用版本核对一致。无新建容器。[本机复查](local-final.txt) · [服务器实例](server-final-container.txt) · [ACL 复查](server-final-device.txt)

v0.0.8 的过渡大修方案纳入用户看到的 Wayland 两阶段启动、依赖扫描、KDE 菜单、宿主输入法，以及项目的包事务恢复、应用生命周期、文件交付和设备/保护缺陷。全量离线结果另记。复杂 MPEG-2 图样的偏差归因、剩余代表用例、D1–D5 与候选构建仍待完成。

当前工作树全量离线检查执行 1161 项，1156 通过、5 项条件跳过，独立保护收尾通过。[全量日志](transition-full-t1/command.log) · [守卫回执](transition-full-t1/receipt.json)
