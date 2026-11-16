# 服务器 V100 两个专用测试实例方案

2026-11-16，待本次新建授权。目标为 G01/G02、G05/G06、G10 的无桌面 CUDA 子项。Ubuntu 24.04.5 服务器以原 `agent` 账号运行。现有 6 个 CID 都已停止且设备列表为空。最近建的 Fedora 实例是 AMD 专用，冻结设备为 AMD render node。两个 V100 需要不同的冻结设备，而且同一管理根只登记一个 Fedora 实例，因此申请两个专用管理根和两个 CID。全程逐张串行。

| 实例 | 原物理根路径 | 容器名 | 必须匹配的实卡 |
|---|---|---|---|
| A | `/home/agent/unifier-hardware-v008-v100-214dwxyS-a/environment` | `ml-ee86a521-fedora44` | `0000:03:00.0` · `GPU-711a11e2-5b33-9a0c-2c9d-4dbfa0ebbca9` · `/dev/nvidia0` |
| B | `/home/agent/unifier-hardware-v008-v100-214dwxyS-b/environment` | `ml-62005bd7-fedora44` | `0000:04:00.0` · `GPU-fca67906-4c5b-f1ed-8396-745631927e19` · `/dev/nvidia1` |

两个根的上级目录经查均不存在。复用缓存 Fedora 44 `registry.fedoraproject.org/fedora@sha256:10667e171df877cedd245b7065238143e09eaae7da4656d3e62eb7c094378011`，镜像 ID `sha256:4b5d9a75c81b96b542ac08b19587efa516da72bf02581908c6178d39de4bb07b`，进客体前核对实际身份和初始库存。

## 无桌面设备范围

- 每实例只授权一个对应的 GPU 节点和共用的 `/dev/nvidiactl`、`/dev/nvidia-uvm`，均 `rw`。宿主的 UUID、PCI、节点 major/minor 来自本轮实测，映射写入创建规格并在启动前复核。[精确映射与驱动 SHA-256](attachment-t1/manifest.json)
- 只读挂载由宿主 root 所有的精确版本 `/usr/lib/x86_64-linux-gnu/libcuda.so.580.178.04` 到 `/run/unifier-cuda/libcuda.so.1`。仅再挂载当前私有执行目录中经哈希核验的探针 ELF 和 `sm_70` cubin，二者分别只读。客体网络模式 `none`，private IPC/PID，rootless，保持默认安全标签。
- 现有系统 CDI 的 UUID 条目会展开额外 DRM 节点、组、钩子和持久服务套接字。现场 Podman 4.9.3 对私有 CDI 搜索目录的支持未获验证。首轮拟用每实例受控的显式设备和精确库挂载，先实现正式创建及启动前 OCI 检查。系统 CDI 文件、宿主权限、驱动和其他实例不涉及配置变更。若仅挂载该库无法运行，记录所需对象和错误证据后再评估增量。

## 执行、资源及恢复

1. 新建前补齐 Unifier 设备请求入口和专项保护器适配，离线验证拒绝额外设备、挂载、钩子、错误 UUID 和改绑。校验保存的 OCI 规格后才允许首次启动；发现不符时保留已创建的唯一 CID 与明细。普通现有实例的 reuse 保护保持原边界。
2. 从前一次准入状态重新核验 boot、身份、镜像、内存、CPU/cgroup、驱动、GPU 温度和活动上下文。256 tasks / 1 CPU / 1 GiB RAM / 128 MiB swap 汇总，内含 384 MiB / 128 tasks 客体；独立监督初始 96 MiB / 20 tasks 并计入宿主准入。保留至少 1 GiB MemAvailable 和项目其余 PSI/确认窗口规则。GPU 至少保留 4 GiB 可用显存且应有可归属的独立测试窗口；GPU 70°C、CPU 80°C 或更严格设备限制触发停止。每张卡先建立有界温度/显存和进程观测，单轮 CLI 120 秒，探针任务最多 10 秒。
3. 通过正式入口先验 A，完全停止、核验 GPU 上下文退出和原非目标实例状态后再验 B。每次只运行一个固定 kernel、一个 block、16 线程，显式设备分配共 640 字节。CUDA API 必须只报告一张卡，UUID、PCI BDF 和 SM 7.0 均一致。读回 16 组整数、FP32、FP64 和 16 个 FP16 输入/FP32 累加的矩阵元素，与 CPU 结果逐项核对。记录实际加载库、驱动 API、原始退出码和上下文销毁结果。
4. 正常使用正式 stop；异常只对本轮精确 PID/starttime、单元和本轮已登记 CID 定向收尾。两个新实例停机保留证据，通过前后快照核验原有 6 个 CID、设备和状态。出现恢复屏障时先走正式恢复流程，旧事务日志和失败取证保留。

本轮只读预检和离线编译已完成。复查时两张卡均有外来上下文，可用显存约 542/668 MiB，低于每卡 4 GiB 的原专项保留量。实机阶段须等相应卡满足准入条件。已批准的旧 Intel/AMD 新建范围和本次性能配额调整权限均不包含这两个新 CID。
