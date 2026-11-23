# Mesa r600 MPEG‑2 量化矩阵修复与实机验收

2026-11-16。原 Fedora 44 专用 CID `2180a83540510175c2d177c0decf0cf852788fbe77d6c6263d98737fd644fa3f`，Radeon HD 7470 Caicos `0000:01:00.0`，`renderD128`。原复杂逐行 MPEG‑2 I 帧仍为 5,330 字节，SHA‑256 为 `d7a2a7aa40d6ec988c1081c2620f4cd9ad58aae2099edd821a81ff74b80aacdb`。沿用原独立软件参考，逐帧门槛为 MAE ≤0.35 且最大差 ≤8。[样本和软件参考指纹](playback-fixtures.json) · [此前探针诊断](../v008-amd-mpeg2-coeff-20261116-wwf28iT5/report.md)

## 驱动修复

在 Mesa 26.2.6 的 `src/gallium/drivers/r600/radeon_uvd.c` 中纠正非对称矩阵的行列取值。UVD 消息的 intra、non-intra 两张矩阵固定用普通 Zigzag 序列写入，系数仍按 `alternate_scan` 标志解码。转置补偿首次构建已修复正常扫描，追加短片说明沿用系数扫描顺序序列化矩阵会继续使交替扫描超差。只修改矩阵序列化顺序后第二次验证通过。[源码补丁](../../fixtures/hardware/mesa-r600-mpeg2-matrix.patch) · [ITU‑T H.262](https://www.itu.int/rec/dologin_pub.asp?id=T-REC-H.262-200002-S%21%21PDF-E&lang=e&type=items) · [两轮对照](verified-playback.json) · [最终数据](verified-playback-alt.json)

源码来自与官方 SHA‑256 一致的 Mesa 26.2.6 归档。构建产物为真正的 r600 VA‑API 驱动，导出 `__vaDriverInit_1_23`。Ubuntu 服务器的私有构建目录使用 GCC 13，编译配置仅保留 r600 和所需 GL 核心与 VA‑API，无窗口系统。编译同一源码时切换补丁来获得配对的未修版和转置修版，最后只增加矩阵固定顺序变更。最终文件和被编译源码摘要已独立比对。[Mesa 发布校验](https://docs.mesa3d.org/relnotes/26.2.6.html) · [两版构建](server-build/pair-t1/command.log) · [最终构建](server-build/alt-pair-t1/command.log) · [最终文件与源码指纹](server-build/drivers-alt.sha256) · [修版驱动文件](server-build/driver-fixed-alt/r600_drv_video.so)

## 普通 VA‑API 解码验收

未修改的独立 FFmpeg 7.1.5 使用标准 `-hwaccel vaapi` 路径，`hwdownload` 将 NV12 读到无桌面的验收输出。通过进程内 `LIBVA_DRIVERS_PATH` 加载每个版本，对比 libva 日志中的实际驱动路径、AMD CAICOS、VA‑API 帧、解码帧数和错误数。Fedora 原装和同源码未修版均逐字节重现历史失败输出。整个过程中送入 FFmpeg 的码流和读回程序保持一致。[首轮原始日志和帧指标](server/result.json) · [最终原始日志和帧指标](server-alt/result.json)

| MPEG‑2 输入 | 原装或同源码未修版 MAE / 最大差 | 最终修版 MAE / 最大差 | 达标帧数 |
| --- | ---: | ---: | ---: |
| 原复杂 I 帧 | 0.873210 / 12 | **0.013672 / 1** | 1/1 |
| 默认矩阵 I/P/B 短片 | 0.803382 / 13 | 0.024977 / 2 | 16/16 |
| 自定义非帧内矩阵 I/P/B 短片 | 1.627909 / 58 | 0.023109 / 3 | 16/16 |
| 自定义非帧内矩阵加交替扫描 I/P/B 短片 | 2.839610 / 50 | 0.023109 / 3 | 16/16 |

转置修版的交替扫描短片为 MAE 2.079230、最大差 37，证明需要第二个改动。最终正常和交替扫描的自定义矩阵短片，硬件输出彼此逐字节相同；其软件参考同样逐字节相同。每段短片含 2 个 I、4 个 P 和 10 个 B 帧。原复杂帧和追加样本共 **49/49** 帧在原容差内。Radeon UVD ring 5 已提交并完成的 fence 从 `0x4d` 到 `0xc2`，再到 `0x137`，开始和结束均无 DRM 客户端残留。[逐帧独立重算](verified-playback-alt.json) · [末次硬件计数](ring-alt-after.txt)

## 构建保护和收尾

- 本机第一次 Mesa 配置遗漏私有头文件重定向，首次 VA 链接也发现需要 GL 核心。修正后，NIR 生成器触发 512 MiB 作业回收压力，按用户授权以 1024 MiB 的独立 T1 阶段完成，实测峰值 726.5 MiB。普通编译随后因宿主压力再次停止，余下构建迁到内网服务器的私有目录，保留相同测试锁、1 核和独立准入。服务器 NIR 仅使用已验收的单阶段提升，其他编译沿用 512 MiB。两次本机保护收尾后 PID 均为 0、cgroup 已撤除。[首次压力回执](compile-with-gl-core-1-t1/receipt.json) · [提升阶段](nir-generation-t1/receipt.json) · [普通编译中止](compile-with-gl-core-2-t1/receipt.json) · [服务器构建准备](server-build/prepare-t1/receipt.json)
- T1 对内存预算取值、宿主保留线和 FUSE 固定上限执行定向校验。复用守卫的 30 项离线测试通过。[保护器回归回执](guard-regression-t1/receipt.json)
- 末次实机工作组峰值 146.0 MiB，宿主可用内存最低约 115.46 GiB。两轮原 Fedora 44 专用实例均恢复停止、PID 0，其他七个服务器实例逐项一致，工作 cgroup 已清理，目标节点 ACL 精确恢复。加载修版仅限测试进程，Fedora 原装软件包保留。验收范围是无桌面 FFmpeg 对这四个 MPEG‑2 码流的 4:2:0 NV12 逐行解码。[实机守卫回执](server-alt/receipt.json) · [ACL 恢复](amd-mesa-alt-9wcqmmzm-orchestration.json)
