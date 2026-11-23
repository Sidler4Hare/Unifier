# Intel Iris Xe H.264 VA-API 与本机 CPU 补测

2026-11-16，Asia/Shanghai。复用 Debian 13 专用实例 `9a6c635b14d4e3ea63b6af2f767420ef55a5191011c29c203525f3cd32088b88`。Intel G07、G08 和 G10 媒体子项 **PASS**。

| 项目 | 结果 |
|---|---|
| 设备与后端 | `/dev/dri/renderD128`、PCI `0000:00:02.0`、i915，Debian iHD 25.2.3 / VA-API 1.22。H.264 High 暴露 `VAEntrypointVLD` 和 `VAEntrypointEncSliceLP`。 |
| G07 硬解 | H.264 High、8-bit 4:2:0、1280×720、8 帧。VLD 读回 NV12 的 11,059,200 字节与独立软件解码逐字节匹配，8 次 VA 提交，FFmpeg 进程 i915 video 引擎数据 5,436,548 ns。 |
| G08 硬编 | VA-API EncSliceLP，H.264、1280×720、YUV420P、8 帧。独立软件解码并按帧与固定 NV12 输入对比，最低 PSNR 66.9 dB，预设门槛 36 dB。8 次 VA 提交，FFmpeg 进程 i915 video 引擎数据 17,190,813 ns。 |
| G10 收尾 | 走正式 Unifier 服务与 Podman 执行适配器；两个进程均退出 0，探针在本轮私有客体路径中的输入和工作文件已清理，同 CID 正常停止、PID 0。 |

[设备能力](hardware/vainfo.stdout) · [命令与独立引擎数据](hardware/result.json) · [输出验证](verification.json) · [实机保护](hardware/receipt.json)。引擎计数对应所观察的进程 DRM fdinfo；采样值用于证明有提交，未用作性能基准。[Linux DRM 客户端计数说明](https://docs.kernel.org/gpu/drm-usage-stats.html)

## 依赖准备及恢复

- 有签名的 Debian 本地 APT 计划为 176 个新增包、88,730,164 字节，零升级、零删除。预取全部通过 SHA-256 核对。[计划](query/result.json) · [预取](prefetch.json)
- 前两次 APT 在原共享路径的模拟分别报缺归档、无法接受归档文件。两次都在真正执行安装前结束，保留正式失败事务。第一轮恢复预检又因事务在正式 stop 后新增了收尾字段而拒绝，修正对账后，两个事务均通过正式恢复，原库存 SHA-256 不变、审计为空、日志 clean。[首次事务](install/package-transaction.json) · [首次恢复](recovery-stopped-record/result.json) · [第二次事务](install-confirmed-files/package-transaction.json) · [第二次恢复](recovery-shared-visibility/result.json)
- 已验证的归档经固定 CID 的受控复制进入客体私有路径，客体再核对 176 份 SHA-256。APT 模拟对齐全部版本后执行本地安装，正式事务 `txnf9d5bef4a9d3` completed / verified；登记库存 306 项。没有扩大共享权限或更改 SELinux 配置。媒体输入也用受控复制并核对哈希，原缓存与证据保留。[受控安装](install-private-copy/result.json)
- 适配前的全仓检查 1147 通过、5 跳过，安装与恢复修订后 67 项相关离线合同通过。媒体输入、容器内 i915 使用证据和最终输出分别核对。实机媒体阶段汇总峰值约 221 MiB、27 tasks，宿主最低可用 2,822,283,264 字节，无保护中止。[离线回归](guard-t1/command.log) · [修订后定向回归](private-copy-fix-t1/command.log)

## 本机 CPU 补测

Intel 媒体验收后继续复用同一 Debian CID。首次宿主与客体各自两线程原子累加均得到 20,000，但客体中文挂载路径使 CPU probe 的 ASCII mountinfo 解析失败。保留旧结果，修正为有字节限额的文件系统路径解码，再通过中文路径回归和同 CID 实机复测。[首次结果](cpu-context/result.json) · [中文路径回归](cpu-utf8-t1/command.log)

- **C03 通过**。宿主与客体原子累加、线程屏障和单调计时均正确。
- **C04 部分通过**。宿主拓扑和亲和性通过；客体 sched affinity 与 proc 状态一致，看到 CPU 0–7、一个 NUMA 节点。客体命名空间未暴露父级有效 cpuset 的两个文件。
- **C05 部分通过**。客体实际读取 `cpu.max=100000 100000`、384 MiB 内存、128 MiB swap 和 128 tasks，与守卫在宿主验证的实际限额一致。短探针 `cpu.stat` 使用量增加，未制造节流负载。C07 PMU 未运行。

[CPU 复测](cpu-context-fixed/result.json) · [保护回执](cpu-context-fixed/receipt.json)。媒体及 CPU 测试均已正式停止，原 CID PID 0，工作 cgroup 和汇总 slice 清退；其他 12 个容器的记录快照与开工一致。宿主 boot ID、KWin PID 和启动时间、Git 索引均对账一致。日用入口仍为 v0.0.7。

随后尝试只读核对服务器 AMD 专用 Fedora 实例。Tailscale SSH 超时，LAN SSH 在建立会话前关闭连接，AMD 阶段未启动。[LAN SSH 错误](amd-media-lan-ssh.stderr)
