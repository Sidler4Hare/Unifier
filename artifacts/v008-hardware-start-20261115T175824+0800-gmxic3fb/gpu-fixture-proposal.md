# 首轮 GPU 测试实例与 AMD 权限申请

方案编号：`v008-drm-20261115a`。状态：**仅提案，尚未批准或执行**。2026-11-15 已做有界只读镜像、CDI、节点权限及目录元数据核验，原始记录嵌入同目录 [JSON 方案](gpu-fixture-proposal.json)。

本次申请两件事：

1. 新增 **2 个专用测试管理根、2 个 rootless 测试容器**：本机 Debian 13 验证 Intel，服务器 Fedora 44 验证 AMD。新实例每轮结束 stopped 保留；旧管理根、实例、数据与设备配置全部保留。
2. 仅对服务器 AMD `/dev/dri/renderD128` 临时授予 `agent（UID 1001）` 的 rw ACL，每个 AMD 阶段结束或中止后恢复原 ACL。

V100 暂只继续代码和离线 CDI 裁剪设计，本次不接入 V100，不修改系统 CDI，不申请其额外设备权限；Arch 代表复核也另行提案。本次两实例不等于整个硬件矩阵完成。后续 V100 如需另建或替换冻结配置，仍提交具体对象，不往本次 Fedora 运行实例临时注入。

## 为什么需要这次例外

当前两端测试 CID 均未配置设备或 CDI。设备是创建时契约，不能给现有 CID 偷加设备。正式产品 `service.py:445–473` 又规定同一管理根每 target 只有一个实例，包含 retained/legacy；换一个 env ID 也不能让同发行版并存。因此申请恰好两个新的专用测试根，并保持产品的单实例契约。

[测试规范第 7 节](../../docs/测试禁令与规范.md) 要求：“**只有用户明确批准这次新建后才执行**”。该次申请同时明确涵盖以下新根和新容器，不把新根当成规避零新增默认值的手段。

## 精确实例

| 项目 | 本机 Intel | 服务器 AMD |
|---|---|---|
| owner | omg，UID 1000 | agent，UID 1001 |
| target / env ID | debian13 / debian13 | fedora44 / fedora44 |
| 管理根 | `/home/omg/文档/多兼容linux机/.lab/hardware-v008-drm-20261115a/environment` | `/home/agent/unifier-hardware-v008-drm-20261115a/environment` |
| 容器名 | `ml-a158811c-debian13` | `ml-93dd6c15-fedora44` |
| 新共享目录 | 上述 `environment` 同级的 `shared` | 上述 `environment` 同级的 `shared` |
| 设备集合 | 仅 `/dev/dri/renderD128:rw` | 仅 `/dev/dri/renderD128:rw` |
| 硬件身份 | `0000:00:02.0 / 8086:9a49 / i915` | `0000:01:00.0 / 1002:6778 / radeon` |
| CDI / desktop / Portal | 均不启用 | 均不启用 |
| 汇总组 | `unifier-tests.slice` | `unifier-bridge-tests.slice` |

两个新根目前均不存在；执行时必须再次核验。创建成功后立即登记唯一完整 CID、UUID、generation 与创建证据，随后固定该身份，不自动重建。原有 Debian CID `e58754f0645ae1ff2924cb09a7264cc821e44893e7589a7fd0ebca09ce5f5e45` 和 Fedora CID `31324dbcefe5262532f4e8ce8d4bf7220a2a31ed8d5cc00f9e747b413d194156` 不替换、不复制文件系统、不改记录。

按本轮盘点，容器总数预计本机 12→13、服务器 5→6；创建前重新盘点并保存全部状态。

两台都使用已有本地缓存基础镜像，`--pull=never`，不 build/commit/export：

- Debian：`docker.io/library/debian@sha256:918311b7b6c4c6f68b232ba516584925f6c78ad82b6fd534b98979df6438e483`；image ID `567e3c17b188e21cd42aa5e1c55e16d8270f880ed8ea321dbd40f993467efc4c`，162,520,266 bytes。
- Fedora：`registry.fedoraproject.org/fedora@sha256:10667e171df877cedd245b7065238143e09eaae7da4656d3e62eb7c094378011`；image ID `4b5d9a75c81b96b542ac08b19587efa516da72bf02581908c6178d39de4bb07b`，382,082,984 bytes。

容器 root 映射到各自宿主普通用户，private IPC/PID、默认 SELinux label、restart=no、无 privileged。只挂各自新建 shared 到相同绝对路径，不挂旧 shared、HOME、用户 runtime、音频、显示或 DBus。网络采用 pasta，仅供新客体正式包管理准备依赖；探针无网络行为，不继承宿主代理。

工具准备候选：Debian 的 Mesa、Vulkan tools、vainfo、Intel media VA 驱动、FFmpeg；Fedora 的 glx-utils、mesa-dri-drivers、libva-utils、mesa-va-drivers。**执行前先解析精确版本、签名来源与体积，生成 package plan**，只用新 CID 内的正式事务入口和已配置的发行版仓库，不自动加第三方仓库或宿主包。超过下列预算则停止该准备项。

## 预算、退出和保留

- 每新 CID：384 MiB RAM、128 MiB swap（Podman memory-swap 总值 512 MiB）、128 tasks、1 CPU。
- 作业汇总：1 GiB RAM、128 MiB swap、256 tasks、1 CPU，含容器、controller、工具与转发；controller 默认 192 MiB/64 tasks。独立监督器另 64 MiB/16 tasks。
- 两机、两容器、不同 GPU、安装/编译/探针全部串行。宿主至少留 1 GiB；按完整新增预算计算的准入门槛为 2,348,810,240 bytes，并满足连续 15 秒 PSI 窗口及 250 ms 独立监测。
- 每用例 ≤120 秒，每阶段 ≤300 秒，一段实机执行 ≤45 分钟；保护中止保留锁，不换 runtime 重试。
- 每台宿主本首轮下载总额 ≤200 MiB、额外写入 ≤1 GiB，包含容器 upper、缓存、shared 与回执。每命令日志 ≤20 MiB，全轮日志 ≤200 MiB；相关文件系统剩余不少于 max(5 GiB, 10%)。不拆批绕过该首轮总额。
- 初始 GPU 样例使用 256×256 离屏帧、payload ≤16 MiB、≤10 次固定 draw/dispatch，无数据依赖的无界循环；首次提交等待上限 2 秒。GPU cgroup 不被当作 GPU 显存/执行时间硬隔离；超时停止后续提交，核验上下文，不 reset 或卸载驱动。
- GPU 温度中止线 70°C，CPU 80°C，遇实际临界阈值更低则下调。AMD 已见 radeon 传感器；Intel 当前只有 coretemp package，GPU 温度与共享内存口径未确认前不做持续负载。AMD 实际显存总量还需核验，预留 max(25%, 128 MiB)，缺值不启动分配。
- 正常退出与中止均核对本轮 PID/starttime/cgroup、GPU 上下文、lease、精确 CID 与非目标事件。每轮恢复 stopped；专项暂停或结束后 stopped 保留。删除任何新旧实例均另行授权，未包含在本申请中。

本机回执路径：`artifacts/v008-hardware-start-20261115T175824+0800-gmxic3fb/gpu-drm-local`。服务器回执路径：`/home/agent/unifier-hardware-v008-drm-20261115a/receipts`。回执不挂入客体。

## AMD 单节点 ACL

只读实测：`/dev/dri/renderD128` 为 `0660 root:render（993）`，PCI `0000:01:00.0`，设备号 `226:128`；agent 当前无 read/write 权限，也不在 render/video 组。原 ACL：

```text
user::rw-
group::rw-
other::---
```

批准后执行者需有设置该节点 ACL 的权限。每阶段先重验 PCI、设备号、owner/mode/ACL；保存 `getfacl -pn` 到 `/home/agent/unifier-hardware-v008-drm-20261115a/receipts/amd-renderD128.acl.before` 并登记哈希，然后只执行：

```text
setfacl -m u:1001:rw /dev/dri/renderD128
```

阶段结束或中止后，对核验为同一设备的节点执行 `setfacl --restore=<上述备份>`，逐项比对原 ACL。若节点身份变了，停止自动恢复并报告，不能对新节点套旧 ACL。不加入用户组，不 chmod 整个目录，不改 udev。Intel 当前 render 节点已是 0666；本申请不改本机权限或 SELinux 全局规则。

## V100 只读发现及后续边界

已有两个准确 CDI 名称：

- `nvidia.com/gpu=GPU-711a11e2-5b33-9a0c-2c9d-4dbfa0ebbca9`，PCI `00000000:03:00.0`。
- `nvidia.com/gpu=GPU-fca67906-4c5b-f1ed-8396-745631927e19`，PCI `00000000:04:00.0`。

驱动 580.178.04，每张 16 GiB。`/dev/nvidia0/1/ctl/uvm/uvm-tools/modeset` 当前为 0666，agent 可读写；不能把 AMD 的权限问题套到所有 CUDA 节点。

但 `/var/run/cdi/nvidia.yaml`（SHA256 `724294e4c4477cb8c78be32c4d960ccf87dca2438206e25d9f3601cdcdde3b2a`）的 UUID 展开还包含各自 card1/2、renderD129/130、additionalGids 44/993、若干钩子和驱动库。全局 edits 包含 `/run/nvidia-persistenced/socket`、MPS 工具及 nvidia-xconfig。完整原文已放 JSON，未执行任何钩子。

这些超出首轮最小 DRM 申请，不能盲目附加权限或持久服务 socket。因此后续先完成 compute-only CDI 裁剪设计、精确 UUID 接入和离线拒绝测试；成熟后再申请明确配置。本次不生成/覆盖 CDI、不接入 NVIDIA、不把 `all` 或索引 0/1 当稳定身份。

## 执行前硬门禁

即便用户批准，本申请也不允许跳过以下条件：

1. 实现并离线验证 GPU 专项保护器：精确一次性创建规格→完整 CID 绑定→资源/设备/显存/温度检查→正常与异常收尾。普通 reuse guard 的 Devices 拒绝保留，不直接删条件放行。
2. 实际创建期间就有独立监督和硬预算；正式 create 内的短时 start/identity/inventory/stop 全程覆盖。不能先裸创建再补限额。新增 fixture 的 384 MiB profile、精确设备和新根都须列入专用策略。
3. 再核对新根不存在、镜像指纹、实时宿主准入、原记录/旧实例状态、设备身份、权限、温度与显存。权限或 SELinux 拒绝即 BLOCKED，不外推为允许宿主全局变更。
4. 依赖版本与体积计划、有限工作量探针、输出 oracle 与上下文退出方案已完成；V100 CDI 接入代码未成熟的状态不被首轮批准自动解除。

本文件没有执行上述新建、包安装、ACL 或 GPU 操作。

