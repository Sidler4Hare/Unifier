# v0.0.8 硬件专项首轮

2026-11-15，Asia/Shanghai。用户授权开始测试、发现问题修复，并明确本轮使用 Sol/内置子代理，不运行 Qwen。本轮状态为 **PARTIAL**：已有 CPU 实证和一项产品修复；GPU 运算、完整硬件接口与 v0.0.9 准备尚未完成。未发布、未部署、未提交或推送，日用版仍为 v0.0.7。

## 已完成的验证

| 机器 / 客体 | C01/C02/C06 指令、启用状态与可移植性 | C03 两线程原子/同步/单调计时 | C04/C05/C07 边界 |
|---|---|---|---|
| i5-1135G7 宿主 / Debian 13 | PASS：SSE4.2、AVX、AVX2、FMA、AES-NI、BMI1/2、AVX-512F 共 8 项 | BLOCKED：补充轮内存准入不足，NO_START | 补充轮未启动 |
| E5-2686 v4 宿主 / Fedora 44 | PASS：上述前 7 项；AVX-512F 正确识别 unsupported 并跳过 | PASS：两线程各 10000 次，原子计数 20000，屏障/计时/线程退出有效 | C04 宿主 PASS、客体 PARTIAL；C05 PARTIAL；C07 NOT_RUN |
| E5-2686 v4 宿主 / Arch | PASS：上述前 7 项；AVX-512F 正确识别 unsupported 并跳过 | PASS：同一原子库和参考答案 | 同上 |

指令探针为同一 SHA256 的 11224 字节 Linux x86-64 基线 ELF，无动态加载器或 libc，使用 CPUID、OSXSAVE/XGETBV 后分派；反汇编确认实际执行相应 opcode。每轮宿主与客体的原始 JSON 逐字相同，两个执行退出码均 0。原子库 9752 字节，无 NEEDED，真实 `lock add`，ctypes.CDLL 释放 GIL；没有用 Python 锁代替原生原子操作。

客体可见 36 个逻辑 CPU、1 个 NUMA 节点，但真实有效 cpu.max 为 `100000 100000`，不等于可同时使用 36 核。客体不可见的父级 cpuset 文件保持缺失，不能补造为读取成功。C05 核验可见限额、短任务前后 cpu.stat 与保护器外层约束/停止；未强制造成节流，不作无限额性能比较。C07 只记录 perf 权限元数据，没有运行 PMU，也未修改宿主权限。单次命令耗时包含 Podman/保护查询，不用于比较 CPU 性能。

原始证据：

- [本机指令对照](cpu-local-debian/result.json) · [保护回执](cpu-local-debian/receipt.json)
- [Fedora 指令](server/cpu-server-fedora44/result.json) · [Arch 指令](server/cpu-server-arch/result.json)
- [Fedora 原子与上下文](server/context-server-fedora44/result.json) · [Arch 原子与上下文](server/context-server-arch/result.json)
- [本机补充轮未启动](context-local-debian/receipt.json) · [独立 CPU 审查](cpu-independent-review.md)

## 发现并修复

1. **产品问题：显式 render node 错误依赖桌面。** 原 service 对所有设备强制 `--desktop`，阻断无桌面的离屏图形/计算。先由 3 项定向用例复现其中 1 项错误，再只放开存在的字符设备 `/dev/dri/renderDN`。自动 `--gpu`、primary DRM、摄像头和 Portal 保持原边界；无桌面时不发现/挂载会话，private IPC 和默认 SELinux label 保持。CLI 帮助同步，冻结设备契约不变。这只证明正式创建路径的离线合同，GPU 实际访问尚未通过。
2. **测试脚本问题：有效错误答案误记 INVALID。** 独立审查发现新 context controller 在解析 JSON 前统一拒绝非零退出，无法把正确性 FAIL 与无效执行区分。修复后保留解析证据，分别处理 FAIL、损坏 JSON/信号和部分元数据。原版本保存在 `cpu-context-controller-before-classification.py`，不覆盖旧证据。

验证记录：

- `baseline-t1`：开工全仓 1070 项，1065 通过、5 条件跳过，退出 0。开工产品树与 v0.0.7 发布清单比较仅版本标记不同。
- `headless-repro-t1`：3 项中 1 项报错，真实复现，命令退出 1，无保护中止。
- `headless-fixed-t1`：53 项受影响合同通过，退出 0；见 [独立设备审查](independent-review.md)。没有重复全量以扩大通过数。
- `context-classification-t1`：旧错误分类复现；修复后错误答案、无效执行、部分元数据分类检查通过。
- `cpu-build-t1`、`cpu-atomic-build-t1`：编译及二进制结构/反汇编核验；未把 T1 当作硬件实测入口。

## 保护与收尾

实机采用现有固定 CID 保护器，控制器 192 MiB/64 tasks，汇总 1 GiB/128 MiB swap/256 tasks/1 CPU；本机既有 Debian 768 MiB，服务器既有 Fedora/Arch 384 MiB，原限额不变。独立监督器 64 MiB/16 tasks，15 秒实时准入、每 250 ms 监测，每轮 120 秒，单探针最多 3 秒。CPU 温度前后核验低于 80°C；指令阶段本机最高观测 62°C、服务器最高 31°C。真实内存/任务峰值和原始退出码见各 receipt。

本机 context 补充轮在 60 秒内未满足含全部新增预算的内存准入线 2348810240 bytes，回执为 `admission failed / NO_START`，工作进程与客体均未启动。保留原尝试及中止锁，没有重试、放宽阈值或停止用户程序。之后只执行服务器原计划中独立准入的用例。

所有已执行 T2 命令退出 0、保护 reason=null，正式 stop 完成。工作单元的 failed/143 来自保护器结束其清理等待，不是探针退出码。最终两端无运行容器：本机仍 12 个、服务器仍 5 个；CID 集合、所有非目标记录和目标配置未变，只有本轮目标的正常启动/停止时间改变。[容器核验](container-final-validation.json)

285 个开工保护文件无缺失；仅本轮两个产品文件、当前状态/剩余台账/硬件计划改变，Git 索引哈希未变。远期规划另有本轮改前副本；新增三份 CPU fixture、设备回归测试和本轮回执。共享目录仅增加本轮命名的短探针文件，不改旧数据。服务器使用小型源码快照，没有新增产品管理根或容器。[保全核验](preservation-validation.json)

## GPU 下一步

宿主清单已确认 Intel Iris Xe、HD 7470/radeon，以及两张 V100-SXM2 16 GB（驱动 580.178.04）。这些是元数据，不算 GPU 功能通过。全部既有容器都没有设备，普通 reuse guard 明确拒绝设备；GPU 专项创建/保护和 NVIDIA CDI 仍需实现。

AMD renderD128 为 0660，agent UID 1001 无读写权限。NVIDIA 的 `/dev/nvidia*` 可访问，但现有 CDI 的精确 UUID 展开还包含 primary DRM、附加组、持久化服务 socket 与钩子，未直接套用或更改系统 CDI。

[可审查的首轮 GPU 方案](gpu-fixture-proposal.md) 精确申请本机 Debian/Intel 与服务器 Fedora/AMD 两个新实例、两个专用测试根，以及仅 AMD 节点的临时 UID ACL 和恢复。新建和宿主权限例外尚未获批，未执行；方案列出镜像、节点、预算、到期停止保留及恢复步骤。V100 和 Arch GPU 不包含在这次两实例申请中，后续单独按最小配置准备。批准也不解除方案中的保护器、依赖体积与有限 GPU 工作量门禁。

下一轮保留事项：本机补充 CPU 用例重新具备准入条件后另立尝试；GPU 精确实例与保护实现；D1 能力协议、D2 NVIDIA 接入、D3 GPU 探针、D4 诊断、D5 统一目标目录；完整候选验包与发布验收。
