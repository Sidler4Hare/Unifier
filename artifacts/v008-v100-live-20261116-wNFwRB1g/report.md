# v0.0.8 服务器双 V100 无桌面 CUDA 验收

2026-11-16，Asia/Shanghai。用户确认继续[两个专用实例方案](../v008-v100-20261116T122628+0800-214dwxyS/fixture-proposal.md)。服务器此前的模型计算上下文已退出，准入时每卡可用显存 16,145 MiB。本轮 V100 逐卡 G01/G02/G05/G06 及 G10 计算子项 **PASS**。使用 Ubuntu 24.04.5、rootless Podman 4.9.3 和锁定 Fedora 44 镜像。

| PCI | NVIDIA UUID | 正式实例 CID | 结果 |
|---|---|---|---|
| `0000:03:00.0` | `GPU-711a11e2-5b33-9a0c-2c9d-4dbfa0ebbca9` | `3f5d75aa825985ae07ea88a0d21ca59e66a37ed815d97a2f90ac4dc1ae317536` | [CUDA 输出](server/a-gpu/result.json) · [保护回执](server/a-gpu/receipt.json) |
| `0000:04:00.0` | `GPU-fca67906-4c5b-f1ed-8396-745631927e19` | `4436c4da2827a1acb2957c1ec5c945474b6b3a28e7b608bd207ff94bacd9dd93` | [CUDA 输出](server/b-gpu/result.json) · [保护回执](server/b-gpu/receipt.json) |

## 改动与验收判据

- 工作树新增 `unifier create <target> --cuda-device <GPU-UUID>`。通过加载中的 NVIDIA 驱动解析 PCI 和节点，创建契约冻结 UUID、PCI、minor 和驱动库路径。只映射所选 `/dev/nvidiaN` 及 `nvidiactl`、`nvidia-uvm`，并只读绑定实际 `libcuda.so.580.178.04`。启动时复核宿主身份和只读驱动路径。测试使用显式计算设备，系统 CDI 条目没有改动。
- 正式服务创建、启动及停止，算例走正式 Podman 执行适配器。独立保护器检查保存的 OCI 配置、只读文件哈希、实际 `/proc/<init>/root/dev`、UID 映射（客体 0 → 宿主 1001）、完整 CID、cgroup、温度和外来上下文。两张卡所在客体都只枚举到一张卡，UUID、PCI、SM 7.0 和加载库路径匹配，无 `CUDA_VISIBLE_DEVICES` 掩码。
- 两张卡均执行同一由 CUDA 12.9 构建的 `sm_70` cubin。580.178.04 驱动报告 Driver API 13000。每次只提交一个 16 线程 kernel，显式设备分配 640 字节；16 组整数、FP32、FP64 和 4×4 FP16 输入/FP32 累加矩阵结果全部与 CPU 参考一致。两次均同步、读回并销毁上下文，结果退出 0。此矩阵验证使用普通 CUDA 核心，Tensor Core 未单独验证。

## 失败记录、离线回归与收尾

- 最初上传的源码快照遗漏 `src/multilinux` 兼容模块，A 在提交任何 Podman 创建动作前退出。原回执为 `NO_START`、命令退出 2、保护原因 null，无容器事件，工作进程与 cgroup 已清退。保留原创建意图与空项目目录，补齐快照并核对六个原 CID 状态后在同一目录一次续跑，正式创建通过。[原尝试](server/a-create/receipt.json) · [正式创建](server/a-create-fixed/receipt.json)
- 全仓 T1 共 1149 项，1144 通过、5 项条件跳过；源码快照续接适配后另跑 64 项硬件定向用例通过。正式创建与 CUDA 四次实机阶段均保护原因 null，工作单元 PID 0、cgroup 已移除；实机汇总峰值约 109.6 MiB / 32 tasks，宿主最低可用 124,071,878,656 字节。使用原计划限额。[全仓](t1/command.log) · [定向](t1-setup-fix/command.log)
- 两个新增 CID 都保留在 stopped / PID 0，服务器原六个 CID 及记录的状态字段与开工快照一致。测试 slice inactive，无本轮工作进程；两卡计算上下文列表为空，均恢复到 16,145 MiB 可用显存。日用入口仍为 v0.0.7，Git HEAD 和索引核对一致。[最终复查](server-final.txt)
