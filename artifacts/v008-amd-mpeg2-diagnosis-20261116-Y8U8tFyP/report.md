# HD 7470 MPEG-2 复杂帧诊断

2026-11-16，原服务器 Fedora 44 专用实例 `2180a83540510175c2d177c0decf0cf852788fbe77d6c6263d98737fd644fa3f`，Radeon HD 7470（Caicos，PCI `0000:01:00.0`，`renderD128`），Mesa 26.2.6 r600，VA-API 1.23。复用原 128×96、5,330 字节的逐行 MPEG-2 Main I 帧与原软件参考。原判据为 MAE ≤0.35、最大差 ≤8。[前次验收](../v008-hardware-resume-20261116-jxwS5tC7/report.md) · [核对汇总](verified-diagnosis.json)

## 诊断结果

| 对照 | 结果 |
| --- | --- |
| 软件参考 | 本机 FFmpeg `auto`、`simple`、YUV420P 转 NV12 和独立构建的 FFmpeg 7.1.5 软件解码均逐字节重现原参考。`int` 和 `xvid` 相对该参考最大差 1。换用任一参考均无法使 Radeon 通过。[软件结果](software-comparison.json) |
| 硬解客户端 | 独立上游 FFmpeg VA-API 与补齐帧起始标志的探针，均与原 Radeon 输出逐字节一致。独立 FFmpeg 明确选用 VA-API 帧，调用轨迹覆盖解码提交和读回。[结果](server-fixed/result.json) · [轨迹](server-fixed/ffmpeg-va.trace) · [FFmpeg MPEG-2 VA-API 实现](https://ffmpeg.org/doxygen/trunk/vaapi__mpeg2_8c_source.html) |
| 同一表面两种 NV12 读法 | 先读取线性 DMA-BUF，再从同一表面调用 `vaGetImage`。两份 18,432 字节结果及前次 Radeon 结果一致，SHA-256 为 `f49b86f92034c5549b99d979af648d425a58c1b36cbcfa5196d0a762cb5c687b`。UVD ring 5 已提交并完成的 fence 从 `0x17` 到 `0x1a`，两端采样均没有 DRM 客户端残留。[结果](server-prime-fdsize/result.json) · [fence 前](ring-prime-fdsize-before.txt) · [fence 后](ring-prime-fdsize-after.txt) |
| 空间分布 | 复杂帧 MAE **0.8732096354**、最大差 **12**，18,432 个样本中有 10,900 个准确。6,656 个参考中恒定的 8×8 块样本全部准确，偏差只落在纹理块。V 的纹理样本 MAE 2.225，较 Y 的 1.169 和 U 的 1.153 高。[分块数据](texture-comparison.json) |
| I420 回读 | 驱动把原生 NV12 转为 I420 后，3,072 对 U/V 值全部相同，原生 NV12 只有 10 对相同。保留原生 NV12 作为验收读法。需要平面布局时可在 CPU 拆分 UV，已验证重打包后逐字节还原原始 NV12。[对照](verified-diagnosis.json) · [Mesa 同版本 GetImage 源码](https://github.com/chaotic-cx/mesa-mirror/blob/mesa-26.2.6/src/gallium/frontends/va/image.c#L447-L505) |

两条 NV12 路径证明偏差已存在于 r600/UVD 的解码表面。Mesa 26.2.6 的导出路径取得已有视频缓冲区并导出资源句柄。r600 向 UVD 提交含 MPEG-2 参数的消息、码流、解码目标缓冲区。结果把故障范围限定到该组合的解码及表面重建，仍无法从外部像素区分驱动提交、固件和芯片内部的责任，也没有进行 MPEG-2 标准符合性裁决。[Mesa 导出](https://github.com/chaotic-cx/mesa-mirror/blob/mesa-26.2.6/src/gallium/frontends/va/surface.c#L1118-L1188) · [r600/UVD 提交](https://github.com/chaotic-cx/mesa-mirror/blob/mesa-26.2.6/src/gallium/drivers/r600/radeon_uvd.c#L795-L838)

## 修正和收尾

- 补齐逐行整帧的 `is_first_field=1` 并做单变量 A/B，输出未变。驱动的 DRM PRIME 2 描述符把两个对象大小填成零，显式线性分配属性也被拒绝。探针复用默认导出的线性表面，用 DMA-BUF FD 查得两平面实际可映射长度 36,864 字节，逐平面验界，读出后核对 18,432 个 NV12 样本。[帧标志修正](amd-mpeg2-frame-start.c) · [首次导出回执](server-prime/result.json) · [复测探针](amd-mpeg2-prime-fdsize.c) · [Linux DMA-BUF 接口](https://docs.kernel.org/driver-api/dma-buf.html)
- 独立 FFmpeg 来自验签通过的 7.1.5 源码，使用隔离 T1 构建。第一次配置因 `pkg-config` 转义工作树路径而失败，改用保护器的短临时目录后构建成功。首次服务器 A/B 控制器因文件名排序断言在启动前退出，修正后再测。各尝试的原始回执均保留。[构建](ffmpeg-ascii-t1/receipt.json) · [初次未启动](server-case/receipt.json) · [修正后运行](server-fixed/receipt.json) · [FFmpeg 签名说明](https://ffmpeg.org/download.html)
- 最终作业退出 0，保护器完成清理，目标 CID 停止且 PID 0，其余 7 个服务器实例身份和状态逐项一致。后台作业 PID 0，临时 Radeon ACL 与初始摘要一致。驱动和已安装产品未改动。[守卫回执](server-prime-fdsize/receipt.json) · [ACL 恢复](amd-prime-fdsize-y8u8tfyp-orchestration.json)

**G07 仍为部分通过。** 块状帧硬解通过。复杂帧完整复现并定位，像素仍超过原判据。探针修正已验收，r600/UVD 输出偏差仍待修复。
