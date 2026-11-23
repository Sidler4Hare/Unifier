# HD 7470 MPEG-2 量化矩阵实机复测

2026-11-16，复用 Fedora 44 专用实例 `2180a83540510175c2d177c0decf0cf852788fbe77d6c6263d98737fd644fa3f`，AMD Caicos `0000:01:00.0`、`renderD128`，Mesa 26.2.6 r600/VA-API 1.23。上轮已验证偏差出现在原始解码表面。本轮原复杂码流与软件参考沿用上轮 SHA-256，门槛仍为 MAE ≤0.35、最大差 ≤8。[上轮回执](../v008-amd-mpeg2-diagnosis-20261116-Y8U8tFyP/report.md) · [样本指纹和系数](fixtures.json)

## 定位

- 首轮在独立 FFmpeg 上实测七个码流。四种稀疏码流的软件像素逐字节相同，实机中改变量化尺度、或显式携带默认矩阵，均不改变硬解结果。均匀矩阵实测最大差 1，其余三种为 14。两个稠密码流分别用 escape 和常用 VLC 表示同一批系数，硬解结果逐字节相同。[软件校验](fixture-t1/command.log) · [硬件逐块测量](coeff-comparison.json)
- libva 的量化矩阵参数按 Zigzag 顺序传入。Mesa VA 前端的逆扫描表把权重摆成转置布局，r600 `get_mpeg2_msg` 使用常规 Zigzag 索引时直接取值。逐项核对 64 个索引后，用转置矩阵计算浮点像素，五组受影响的控制样本实测与该模型的最大差均为 1。[libva 矩阵声明](https://github.com/intel/libva/blob/2.23.0/va/va.h#L3024-L3048) · [VA 转换](https://github.com/chaotic-cx/mesa-mirror/blob/mesa-26.2.6/src/gallium/frontends/va/picture_mpeg12.c#L29-L39) · [r600 提交](https://github.com/chaotic-cx/mesa-mirror/blob/mesa-26.2.6/src/gallium/drivers/r600/radeon_uvd.c#L468-L485) · [扫描索引](https://github.com/chaotic-cx/mesa-mirror/blob/mesa-26.2.6/src/util/vl_zscan_data.c#L47-L58) · [模型对照](transposed-model.json)

## 补偿实测

用同一个受限探针逐组 A/B。两次读取同一个 `.m2v`，唯一变更为 `VAIQMatrixBufferMPEG2` 中 intra 矩阵的行列互换。所有标准模式输出逐字节重现独立 FFmpeg VA-API 的结果。均匀矩阵是对称对照，补偿前后完全相同。[探针](amd-mpeg2-matrix-ab.c) · [完整 A/B](verified-matrix-ab.json)

| 样本 | 标准 MAE / 最大差 | 转置补偿 MAE / 最大差 |
| --- | ---: | ---: |
| 原复杂 I 帧 | 0.873210 / 12 | **0.013672 / 1** |
| 稀疏默认矩阵 | 0.871745 / 14 | 0.013672 / 1 |
| 稀疏显式默认矩阵 | 0.871745 / 14 | 0.013672 / 1 |
| 稀疏均匀矩阵 | 0.013672 / 1 | 0.013672 / 1 |
| 密集 VLC | 0.499023 / 3 | 0.014648 / 1 |

原帧转置补偿后有 18,180 / 18,432 个样本与软件参考完全一致。五个样本的补偿路径均满足原判据，原驱动标准路径的复杂帧仍为 FAIL，AMD G07 为 PARTIAL。补偿位于单帧探针，驱动包修复和普通播放路径的验收仍开放。证据范围限于这些逐行 MPEG-2 Main I 帧。上轮 I420 单列，当前复测使用 NV12。

## 收尾

- 两次实机分别执行七次、十次解码，Radeon UVD ring 5 提交和完成 fence 从 `0x1a` 到 `0x2f`，再到 `0x4d`。A/B 作业峰值 115.25 MiB，服务器最低可用内存 115.56 GiB，守卫未中止。[系数守卫](server/receipt.json) · [A/B 守卫](matrix-server/receipt.json) · [末次计数](ring-matrix-after.txt)
- 原实例停止，PID 0。其他七个现有实例身份与状态一致，无增删。末次工作单元 PID 0、cgroup 已撤除，DRM 客户端采样为空，只有本轮的临时设备 ACL 被添加并精确恢复。未运行包事务或更换驱动。[收尾状态](matrix-server/cleanup.json) · [ACL 回执](amd-matrix-wwf28it5-orchestration.json)
