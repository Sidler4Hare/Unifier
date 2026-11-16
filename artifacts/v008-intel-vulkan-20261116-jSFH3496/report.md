# Intel Iris Xe Vulkan 离屏图形与计算验收

2026-11-16，Asia/Shanghai。**G04 图形与计算、G10 图形/计算入口与收尾通过。** 复用本机 Debian 13 专用 CID `9a6c635b14d4e3ea63b6af2f767420ef55a5191011c29c203525f3cd32088b88`，沿用原物理根、原 384 MiB 客体限制和 128 tasks。未安装新包或创建实例。

## 实测

| 项目 | 结果 |
|---|---|
| 实际 GPU | 客体核验单一集成物理设备 Intel Iris Xe (TGL GT2)，PCI `0000:00:02.0`、硬件 ID `8086:9a49`。Vulkan 报告的 render major/minor `226:128` 与客体以实际 UID 打开的 `/dev/dri/renderD128` 一致。 |
| 实际驱动 | 只选客体的 `/usr/share/vulkan/icd.d/intel_icd.json`，验证加载 `/lib/x86_64-linux-gnu/libvulkan_intel.so`，Vulkan driver ID 为 Intel Mesa ANV，Mesa `25.0.7-2+deb13u1`，Vulkan `1.4.305`。OCI 无桌面挂载，未创建 surface，隐式 layer 禁用。 |
| 图形 | 3 顶点绘制经过图形管线，scissor 内为红色，外部附件保持绿色。完整 32×16 RGBA8 的 512 像素逐字节匹配。128 红、384 绿，读回 SHA-256 `6f9c95bdf235d8299b13102c59f1a796e62bff71348b869a190a2c1c6cd56956`。 |
| 计算 | 1 个 workgroup、16 lanes，16 个 uint32 和 16 个 FP32 结果全部与独立 CPU 参考逐字节一致。读回 SHA-256 `6eb79cd358919b2deaed42f9dc43778ea707ff5c12c0d5cdd3eede5afd587e88`。 |
| 同步与退出 | 一条队列、一次提交，图形传输与计算写入同步后读回。显式分配 6,368 字节，探针删除 Vulkan device/instance 后退出 0，stderr 为空。 |

原始 [Vulkan stdout](run/vulkan.stdout)、[逐项验证结果](run/result.json)、[客体节点](run/guest-device.json) 和 [监督回执](run/receipt.json)。PCI、DRM 对应依据是运行时查询的实际属性。[Vulkan PCI 属性](https://docs.vulkan.org/refpages/latest/refpages/source/VkPhysicalDevicePCIBusInfoPropertiesEXT.html) · [Vulkan DRM 属性](https://docs.vulkan.org/refpages/latest/refpages/source/VkPhysicalDeviceDrmPropertiesEXT.html)

## 保护与回归

- 新增固定 Intel Vulkan 保护阶段，只接受已核验 ELF、固定 Vulkan 环境和一份精确 guest 临时路径。标准服务创建/停止模型保持原状，本轮通过正式服务启动、执行适配器提交探针并正式停止。
- 首次 T1 已生成 ELF 和三段 SPIR-V，脚本随后因把 `rg` 写成不存在的 `/usr/bin/rg` 退出 1。保留该回执。新 T1 重新验证三段 SPIR-V、ELF 依赖及 glibc ABI 后，全仓 **1146 项通过、5 项条件跳过**。ELF 依赖仅 `libvulkan.so.1` 与 `libc.so.6`，最高所用 glibc ABI 2.34，客体登记为 2.41。 [首次命令日志](build-t1/command.log) · [独立复核与全仓回归](verify-t1/command.log)
- 实机保护原因 null，汇总内存峰值 195,633,152 字节，27 tasks，宿主最低可用 2,800,353,280 字节。核显采用 CPU package 温度代理，准入 56°C，最后样本 59°C。该用例为正确性验证，24 秒服务时长包含独立准入和生命周期。
- 探针文件移除，原 CID 停止且 PID 0，工作单元与汇总 cgroup 清退。容器数仍为 13，其他 12 个的原生快照字段完全一致。登记身份、包库存和 Git 索引一致。日用入口仍为 v0.0.7。[收尾](run/cleanup.json) · [原生终态](run/native-after.json)
