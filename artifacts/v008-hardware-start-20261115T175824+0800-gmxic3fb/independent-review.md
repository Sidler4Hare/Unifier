# v0.0.8 headless 改动与实机路径独立审查

2026-11-15。仅执行有界只读查询；未测试、编译、启动容器、SSH 或调用模型客户端。依据当前 AGENTS.md、测试规范、硬件专项计划、改前副本及本轮 inventory。主控提供的定向测试结果只作为其执行报告，本审查没有重跑。

## 代码审查结论

未发现本轮最小补丁引入的 P0/P1/P2 回归或必须先修的缺失约束。

- `src/unifier/service.py:362–373` 只放开显式、存在且为字符设备的 `/dev/dri/renderDN`；自动 `--gpu`、Portal、camera、primary DRM 仍要求 desktop。路径白名单未扩大到任意设备或 NVIDIA CDI。
- `src/unifier/service.py:384–421` 的无桌面分支保持空 session mounts、默认 SELinux label 和默认 private IPC；没有调用桌面发现。用户显式 IPC 参数的既有语义未改变。
- `src/unifier/cli.py:66–68` 的说明对应当前能力。`runtime.py:418–421` 继续逐节点生成 `--device path:path:rw`；现有环境的设备配置仍是替换契约。
- `tests/test_hardware_devices.py:19–65` 覆盖成功路径、无会话导入、默认隔离、持久契约、旧边界拒绝和非法设备拒绝；结合主控报告的 53 项相关测试通过，当前补丁无需额外全量回归。GPU 实际能否使用、DAC/SELinux 和后端库兼容性仍应通过后续实机用例证明，不能从 fake PASS 推导。

## CPU 最短可行复用路径

1. 首批只选择本机 Debian `e58754f0645ae1ff2924cb09a7264cc821e44893e7589a7fd0ebca09ce5f5e45` 与服务器 Fedora `31324dbcefe5262532f4e8ce8d4bf7220a2a31ed8d5cc00f9e747b413d194156`，两机串行；服务器 Arch `aa38e5466b9665765aefe9931f5550eec78631bfa820121469391845cef7e430` 留作后续跨客体代表复核。保持各自原管理根、UUID/generation 和共享挂载，不创建环境、不改配额。
2. 宿主编译放入 T1 保护；已知答案小型静态二进制及上下文脚本按哈希复制到本轮私有目录。宿主原生执行和客体执行都放在正式 T2 固定实例保护器 controller 的覆盖内，顺序执行，不把 T1 离线入口作为真实硬件验收的授权替代。
3. 使用现有 `reuse_test_guard.make_service(spec)`（`tools/reuse_test_guard.py:521–560`）建立固定 runtime；正式 service start/run/stop，或固定 runtime 的精确 CID cp/exec。通过已核准共享挂载可少一次 cp；仅新增本轮唯一文件名，禁止覆盖旧共享数据。正式 service run 当前要求先 start（`src/unifier/service.py:758`）。
4. 本机 fixture 保持 768 MiB RAM/128 MiB swap/192 tasks；服务器使用已实现的 `remote-package-20261112` 精确绑定，保持 384 MiB RAM/128 MiB swap/128 tasks（`reuse_test_guard.py:45–66`），默认 controller 192 MiB 足够作为小探针起点。总组仍 1 GiB/128 MiB swap/256 tasks/1 CPU，保护器另 64 MiB/16 tasks；每轮新 spec/new receipt、≤120 秒探针阶段、原生与客体串行。
5. 启动前重新核对镜像 digest、完整 CID、原记录、挂载、lease/bridge/恢复屏障、空专用 cgroup、CPU 温度和实时准入。`info()` 已校验身份/资源/设备/挂载，`launch()` 已拒绝运行态和存留租约，但并不读取 spec 的 `image_digest`，所以该字段不能仅写入后便当作已校验；控制器应将 inventory 的镜像 ID 与原锁定镜像关系作明确核验。服务器保护器和源码快照必须在服务器自身运行并固定指纹，SSH 断开不应失去监督。
6. 每轮检查原生与客体输出的同一 oracle、实际 ISA/CPUID 与不支持项、进程退出、lease 与 CID 恢复 stopped、非目标事件。首轮探针只证明其覆盖的 CPU 用例，不宣称整体 GPU 或完整 C01–C06 已通过。

## GPU 执行阻断与具体方案的数据

以下是当前执行阻断，不是最小 headless 补丁的回归。

| 优先级 | 已确认阻断 | 需要完成的具体工作 |
|---|---|---|
| P1（实机前） | 两端当前所有 CID `devices=[] / cdi=null`；设备属于创建时冻结契约 | 提交明确数量、发行版、原管理根与旧实例保留方式、新实例名称/代际、镜像 digest、精确节点/CDI、资源、挂载、到期 stopped 保留和恢复方案；按规范第 7 节获得这次新建/替换的授权，不能直接往旧 CID 注入设备 |
| P1（实机前） | `reuse_test_guard.py:389–404` 明确拒绝非空 Devices；`reuse_guard_policy.py:82–113` 拒绝 DRM/桌面 bind | GPU 专项适配须按完整 CID 与批准配置核验 devices、CDIDevices、映射实际节点/库和无桌面 socket，加入设备身份、显存、温度、超时与退出检查；不能删掉原保护器的拒绝条件或仅用 CPU cgroup 宣称 GPU 受硬限额保护 |
| P1（HD 7470 前） | `/dev/dri/renderD128` 为 `0660 root:993`，主控核验 agent groups 不含 993/44 | 先选择并批准精确权限方案。`keep-id` 或单纯传 `--device` 不会赋予宿主本来没有的访问权；若采用仅该节点对 agent UID 1001 的临时 rw ACL，先记录现 ACL、确切应用/恢复动作与结束条件，不改整个 `/dev/dri` 或全局 mode。组成员方案另需明确宿主账号变更及容器 supplementary-group 传递，不可假设自动继承 |
| P1（V100 前） | `runtime.py:419` 仅接受 DRM/video，尚无 NVIDIA CDI；DRM render node 本身不能替代 CUDA 的 NVIDIA 驱动设备与库 | 完成精确 UUID 的 CDI 契约、可信 CDI 内容/哈希、实际展开设备/库权限检查与离线拒绝测试。DRM render 权限不足不等于 CUDA 一定不可用，CUDA 应另外检查 CDI 所需 `/dev/nvidia*` 的有效权限 |
| P1（GPU 计算前） | 当前 GPU 空闲/低温快照不证明后续持续散热和退出能力 | 按专项计划 §6.2 登记实际温度阈值、显存余量、外来进程、散热条件，固定输入/分配/提交次数、有限执行时间和上下文收尾；V100 历史 PWM 映射未确认，不能自行改风扇或功耗解除此项 |

当前已知的精确设备数据：

- 本机 Intel：PCI `0000:00:02.0`，ID `8086:9a49`，i915，`renderD128` 当前 `0666 root:105`。无需暴露 card1；仍应核对 SELinux 实际访问，不自动放宽全局策略。
- 服务器 AMD：PCI `0000:01:00.0`，ID `1002:6778`，radeon，`renderD128` 当前 `0660 root:993`。
- V100 A：PCI `00000000:03:00.0`，UUID `GPU-711a11e2-5b33-9a0c-2c9d-4dbfa0ebbca9`；V100 B：PCI `00000000:04:00.0`，UUID `GPU-fca67906-4c5b-f1ed-8396-745631927e19`。两张各 16 GiB，驱动 `580.178.04`；精确 CDI 名称及其展开内容尚需核验，不能用索引 0/1 或 `all` 代替身份。
- 首轮主测可按本机 Debian、服务器 Fedora 各一个 GPU 配置设计；服务器若固定批准 AMD 与两张 V100 的精确集合，探针仍逐设备运行并证明未选设备没有本轮工作。该首轮配置不自动满足后续 Arch GPU 复核；完整计划须明确是否还需要每宿主一个 Arch GPU 配置，不能把首轮两实例当作整个矩阵的新建数量。

本回执不授权任何新建、权限、宿主设置或部署操作。
