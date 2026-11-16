# v0.0.8 Intel / AMD DRM 专项

2026-11-15，Asia/Shanghai。本轮 Intel / AMD 设备访问及小型离屏绘制已完成，按用户要求暂停；v0.0.8 整体状态仍为 **PARTIAL**。用户本轮批准两个专用测试根及容器、AMD 单一 render node 的临时 UID ACL 并恢复；随后批准根据测量适当增加服务器预算。两端负载串行，不运行 Qwen。日用安装仍为 v0.0.7，未发布、部署、提交或推送。

## 实例与授权

| 主机 / 客体 | 唯一新 CID | 设备 | 状态 |
|---|---|---|---|
| 本机 / Debian 13 | `9a6c635b14d4e3ea63b6af2f767420ef55a5191011c29c203525f3cd32088b88` | Intel Iris Xe，PCI `0000:00:02.0`，`/dev/dri/renderD128` | 正式登记、设备访问、包事务恢复、离屏绘制及读回通过；已停止 |
| 服务器 / Fedora 44 | `2180a83540510175c2d177c0decf0cf852788fbe77d6c6263d98737fd644fa3f` | HD 7470 / radeon，PCI `0000:01:00.0`，`/dev/dri/renderD128` | 同 CID 正式登记、设备访问、依赖安装及硬件离屏绘制通过；已停止，ACL 已恢复 |

精确镜像、根、挂载、预算及退出方案见 [原批准方案](../v008-hardware-start-20261115T175824+0800-gmxic3fb/gpu-fixture-proposal.md)。不删除或重建这两个实例；V100/CDI 与 Arch GPU 不在这次新增实例范围内。

## 已核实的问题与处理

- Intel 创建时，保护器将同一缓存镜像的 manifest 别名误判成镜像不一致；随后发现子进程 `C` locale 无法处理中文共享路径，以及失败启动留下的 Podman 默认环境字段。逐项修正后，复用同一 CID 完成正式身份、库存及停止态登记，旧证据全部保留。产品新增 `resume_create` 只接受精确 CID、没有启动历史的 headless rootless 创建态；普通 create 仍拒绝替换已有容器。
- `intel-open` 通过实际客体 UID 打开 render node，节点与 PCI 身份对应。它只覆盖 G01 的映射和访问部分，不代表 EGL 或运算已通过。
- Debian 包准备在 LLVM 解包期间触及 384 MiB 客体硬限额，保护中止，OOM 为 0，原安装不能记作通过。原事务 `txne70ac1f03324` 与中止证据保留。
- 53 个缓存包共 70,470,836 字节全部与原计划 SHA-256 一致。LLVM 包的 XZ 数据有 6 块；在原 384 MiB 硬限额内改用单线程解压，并收紧客体 `memory.high` 至 256 MiB。重解包、原六项精确版本的离线缓存安装及 `dpkg --audit` 均退出 0。最终库存核验因可选推荐包 `libgpm2` 未补装而报错；其余 52 包精确安装，后续另立只读对账及正式恢复，原失败回执不改写。[原生修复证据](intel-native-repair/native-maintenance.json)
- AMD Podman 查询实测至少需要 15 个原生线程；`podman info` 的 T1 作业峰值为 75,317,248 字节。按用户授权将服务器监督器调整为 96 MiB / 20 tasks，加独立 root ACL 看门狗后共 112 MiB / 24 tasks；当时客体和工作负载预算保持原值。之后创建唯一 AMD CID，但 Podman 4.9 使用 RFC3339Nano `Time`，保护器原只识别本机的 `timeNano`，误中止；等价时间解析及同实例续接现已修复并验收。

AMD 的事件时间、字符串 Entrypoint、后续 inspect 省略 `StoppedByUser` 的等价形式已修正并定向验收。服务器只读核对原始创建、唯一启动/停止、无后续 exec、完整 CID、镜像/设备/资源及当前停止态后，以专用一次性入口调用正式服务完成原实例初始化；没有扩大通用 `resume_create` 的未启动要求。[初始化编排及 ACL 恢复](amd-bootstrap-183930-orchestration.json) 的阶段退出 0，保护原因 null。登记 UUID 为 `a1e18eb1-72f6-4d99-8178-a4c88cb0e213`、generation 1，初始 RPM 库存 147 项。正常停止，worker PID 0，节点 ACL 恢复为原 SHA-256 `0dd60db424616cea9e9317a9191fff86619ce12819e1a068e8e628fc51c05f6c`。

Intel 正式恢复现已通过。[只读对账](intel-native-repair-reconciliation.json) 保留原生修复的 FAILED，仅精确解释推荐包缺席；[正式恢复结果](intel-package-recover-source/package-recovery-result.json) 的 audit 为空，原生数据库 journal clean，原生/登记/停止态库存 SHA-256 为 `dea12446c5d0f4bc25d0f6a4734038e0b6b1ad668e265822e0a18c437a4733fd`。原事务终止为 failed 并带恢复记录，恢复屏障解除，没有把原中止的安装改成成功。正式停止，worker PID 0、cgroup 已移除。前一个恢复尝试因对账源文件路径误写在预检阶段 NO_START，修正精确路径并用真实证据交叉验证后才新建尝试，旧回执保留。

## Intel 首次真实 GPU 结果

[intel-render](intel-render/result.json) **PASS**，保护原因 null、命令退出 0。使用精确 GBM fd、surfaceless EGL 1.5、Mesa `iris`；renderer 为 `Mesa Intel(R) Iris(R) Xe Graphics (TGL GT2)`，OpenGL ES 3.2 / Mesa 25.0.7-2+deb13u1，EGL 返回同一 render node。256×256 RGBA 固定红/绿图案全部像素一致，262,144 字节读回 SHA-256 为 `0874bcce322a1fd4c5adc518624caee24dcef001ea415d0f1e926a3310a9b57c`。客体探针约 1.21 秒，含执行开销；不是性能基准。正式 stop 和保护收尾均确认同一 CID 停止、worker PID 0、cgroup 清退。这覆盖本机 Debian 的 G01 与 G03 小型无桌面路径，不覆盖 Vulkan、视频或 CUDA。

## AMD 设备访问与依赖准备

[amd-config](server/amd-config/result.json) 中 G01 访问子项 **PASS**：客体 UID/GID 0（映射宿主 agent UID 1001）仅见 `/dev/dri/renderD128`，实际以读写方式打开并关闭，PCI 回读 `0000:01:00.0`。四个原生命令均退出 0、保护原因 null，正式停止并恢复 ACL。整体 config 结果为 BLOCKED：DNF5 的主/仓库配置标题及裸未设值字段不符合最初 INI 假设。解析器现已修复，六项离线用例及真实配置输出对账通过；原失败回执保持不变。[有限缓存准备方案](fedora-preparation-plan.md)

`amd-metadata-vet` 从三个固定 Fedora 官方 HTTPS Metalink 取得共 28,348 字节。旧 updates 的主摘要已过期，首次校验正确拒绝；当前官方响应中的唯一精确备用快照仍认证该副本。随后 `amd-cache-alternate` 在明确的备用快照路径校验 12 个原始元数据文件（49,794,425 字节），只允许离线依赖查询，不将它当作最新仓库或直接安装准入；相关 17 项离线用例通过。

`amd-package-query` 将这 12 个文件导入新实例并回读校验成功，但随后 DNF `--cacheonly --assumeno` 求解触发 **GUEST_INVALID_HARD_LIMIT**，原生退出 137。没有执行 RPM 安装事务。汇总峰值 504,885,248 字节、宿主可用内存最低 118,773,911,552 字节。保护收尾确认同 CID 停止、worker PID 0、工作 cgroup 消失，root ACL 已恢复。此结果为 **INVALID**，不能判定 AMD 图形失败。旧触发时的客体 OOM 计数未保存，不能由 native init 的 `OOMKilled=false` 推断所有客体进程均无 OOM；保护器现已补充触发样本落盘。

按用户允许适当提高服务器预算的授权，[一次复试方案](amd-guest-budget-adjustment.md) 经 87 项相关离线检查和服务器真实停止态只读交叉核验后执行。[amd-query-memory](server/amd-query-memory/result.json) **PASS**：仅这次已核验的客体 scope 临时使用 768 MiB / memory.high 640 MiB，1 GiB 汇总、CPU、swap、线程、容器和时间上限不变；保存配置始终为 384 MiB。重验 12 个 raw 文件，在同一新 CID 的私有缓存硬链接，保留七个旧派生缓存不复用。离线求解及尺寸查询完成，得到 28 包、下载 67,971,267 字节、安装 274,202,076 字节的精确闭包。正式停止、临时 scope 消失、保存配置和非目标状态核验通过，ACL 恢复。结果仍为归档查询，不直接放行安装。

现场 DNF5 的 querytags 不含包 checksum，`location` 为完整镜像 URL。[有界 primary 提取器](fedora-primary-checksums.py) 保留原 URL 并精确归一化路径，从已认证的压缩元数据流式核对 open-size、open-checksum、NEVRA、路径和尺寸，提取 28 包 SHA-256。[提取结果](fedora-primary-inputs/primary-checksums.json) VERIFIED，文件 SHA-256 `5f7627e03a2ccf2d47c938ade42ada547e5101a4a3701449f384d8d29f4cfdbf`。本地七项拒绝与实际查询格式用例通过，服务器受保护 T1 实际提取退出 0；使用本轮私有、精确哈希的 102,760 字节 libzck 副本，无宿主安装。随后完成了下述 RPM 预取与原生签名核验。


首轮 `fedora-package-prefetch` 取得 26/28 个包，`python3-libs` 接收 8,420,948 / 10,672,326 字节后触及单文件 30 秒期限（curl 28）；容器未启动，无资源中止。依据实测速率，对剩余两个包各做一次最多 60 秒的续取，仍受原阶段和累计预算限制。原部分文件以同 inode 保全，原失败回执不改写。[续取结果](server/fedora-package-prefetch-continuation/result.json) VERIFIED：28 包、67,971,267 字节全部与已认证 primary 的 SHA-256 一致；测得累计应用层下载和记录的响应头 76,460,698 字节，下载预算预留 79,081,447 字节、写入预留 505,011,436 字节。仍未启动容器，worker PID 0，ACL 恢复。相关联合 T1 共 89 项通过，其中含 5 项续取用例。

[首次离线安装](server/amd-package-install/result.json) 保留为 **FAIL**。28 包已复制入专用客体缓存并逐包回读 SHA，原生 `rpmkeys` 验证全部 Fedora 44 完整密钥指纹及强摘要，离线求解与原 28 包精确闭包一致。随后正式事务 `txn124e64406e4c` 的原生命令退出 2：测试适配器误用了现场 DNF5 不存在的 `package_gpgcheck` 配置名，现场实际为 `gpgcheck` / `pkg_gpgcheck`。RPM 库存前后均为 `4fbc88c890ab44ee974f34584be338afd98956bdb78224fc6c8c05b0bb722801`，content_revision 未变化，原生数据库 audit 为空，事务 failed / recovery_required=false；没有发生 RPM 安装。保护原因 null，正式停止及 scope 消失，保存限额仍 384 MiB，ACL 恢复。参数已修正并以 85 项相关离线回归及服务器停止态核验通过。随后 [同 CID 续接](server/amd-package-install-retry/result.json) 中，新参数的只读配置/求解、现有 28 包 SHA 与签名重验全部符合预期；提交前因官方认证剩余不足 150 秒而拒绝，没有新增包事务。原执行回执为 FAIL，这里归类为时效准入阻断，不是再一次原生安装失败。该实例再次正式停止、scope 消失、ACL 恢复。新鲜官方 Metalink 再认证单独进行，旧认证截止时间与失败回执不改写。


[官方再认证](server/fedora-metadata-refresh/result.json) VERIFIED：三个官方 HTTPS 响应重新认证同一份 repomd / 原 28 包快照，新增 Metalink 响应体 28,348 字节、已记录响应头 1,128 字节，没有新增 RPM 下载。新认证时效独立为 22:31:08；旧截止时间不变。此前下载及失败片段的预算全部继承，累计含已记录响应头 76,490,174 字节，下载预留 81,047,527 字节，写入预留 511,171,820 字节。三项独立回归、86 项守卫相关回归和服务器真实停止态核验通过。

[正式离线安装](server/amd-package-install-refreshed/result.json) **PASS**：事务 `txn86c84e519723` completed / verified，原生退出 0，28 个精确版本全部安装。`rpm --verifydb` 两路输出为空，journal clean，完整原生安装后库存与正式登记库存一致：175 项，SHA-256 `ece7e7237cb7b60e6f6e12eb84319bafeece265064c9839de351e11b86cbdeeb`。正式记录 stopped / verified、无待恢复事务，原 failed 事务保留。停机后独立解析原生 RPM 数据库仍为 NOT_RUN（宿主无 rpm binding），不扩大为三路原生库存验收；事务的 full-init 系统集成仍为 unverified。汇总峰值 554,921,984 字节、33 tasks、宿主最低可用 124,476,432,384 字节，保护原因 null。临时 768 MiB scope 已消失，保存配置仍 384 MiB；非目标容器和宿主包清单未变化，ACL 恢复。AMD 实际绘制结果见下节。


## AMD 首次真实 GPU 结果

[amd-render](server/amd-render/result.json) **PASS**，保护原因 null、命令退出 0。GBM fd 与 EGL 回读均为精确 AMD `/dev/dri/renderD128`，PCI `0000:01:00.0`；Mesa `r600`，renderer `AMD CAICOS (DRM 2.50.0 / 6.8.0-149-generic)`，EGL 1.5、OpenGL ES 3.1 / Mesa 26.2.6。两次固定 clear 的 256×256 RGBA 图案全部像素正确，262,144 字节读回 SHA-256 `0874bcce322a1fd4c5adc518624caee24dcef001ea415d0f1e926a3310a9b57c`，与 Intel 的已知答案一致。探针约 0.98 秒（含执行开销，不是性能比较）。radeon 原生 ioctl 确认显存总量 1 GiB，提交前空闲 1,070,190,592 字节，预留 256 MiB。

本阶段汇总峰值 127,221,760 字节、31 tasks，宿主最低可用 124,916,146,176 字节。使用原保存的 384 MiB 客体预算。正式停止和监督收尾均确认同一 CID PID 0、worker PID 0 / cgroup 为空；[独立 ACL 恢复](amd-render-183930-orchestration.json) live_verified=true，原 ACL SHA-256 不变。仅据此关闭 AMD / Fedora 的 G01 映射与实际访问、G03 小型无桌面硬件绘制，不推导 Vulkan、视频、ROCm 或 V100 CUDA 支持。

## 证据与验收边界

- 完整离线回归 1115 项：1110 通过、5 条件跳过。该作业后续源码打包因一个未使用的旧符号链接退出 1；这是打包失败，不改写测试结果。后续专门修复并验收打包路径。
- `native-repair-t1`：102 项相关回归通过，并完成 53 包缓存哈希校验；保护状态 PASS，子命令退出 0。
- `reconciliation-t1`：99 项通过；`recovery-source-t1`：26 项通过，并直接核验完整真实历史证据链。两批均退出 0、无保护中止。
- 原生修复作业采样峰值：客体 268,611,584 字节、汇总 430,477,312 字节；宿主可用内存最低 2,643,714,048 字节。汇总硬限额、OOM、任务数事件均为 0。仍保留最终库存断言失败，不能记为整个阶段 PASS。[资源记录](intel-repair-metrics.json)

CPU 首轮结果仍见 [CPU 回执](../v008-hardware-start-20261115T175824+0800-gmxic3fb/report.md)。本记录不关闭 Vulkan、CUDA、视频、跨客体代表项、完整硬件协议及 v0.0.9 公共接口；这些结果逐项补充，不由设备节点存在推导支持。



## 最终保全核验

[只读收尾回执](final-preservation.json) **PASS**：本机 13 个、服务器 6 个容器，分别只比开工时新增已批准的唯一 Intel / AMD CID。12 个本机和 5 个服务器非目标实例的状态、启停时间及登记配置全部一致。两个专用实例均为 stopped / PID 0、保存内存限额 384 MiB；不删除测试根或缓存。

288 个开工保护项中 282 个原字节不变，6 个变化仅为本轮允许的 `runtime.py`、`service.py`、`reuse_test_guard.py` 与三份状态/计划文档；无缺失或不可读。Git HEAD 和索引原 SHA 保持，`git diff --check` 通过。日常入口仍指向 `0.0.7-6d08eadb8142`；此前 [发布文件校验](daily-release-verification.json) 的 326 个条目全部一致。未发布、部署、提交、推送或修改日常安装。

## 今日暂停点

2026-11-15 用户要求“接下来一轮做完就先暂停结束”。AMD 绘制后停止新增测试，只进行只读保全核验和文档整理。两个新测试根和原唯一 CID 保留，不删除；所有历史失败、中止、事务、缓存和恢复记录保留。日常安装 v0.0.7 未部署新代码，Git 索引不改动。

下次继续项：CPU 资源行为与本机未运行项、Intel Vulkan/计算/媒体、AMD 可用视频与不支持诊断、V100 compute-only 接入及两张卡的 CUDA 算例、Arch 代表项、D1–D5 和 v0.0.9 公共接口。V100 / Arch 所需额外实例或设备配置不由本次两实例批准自动授权；先完成其具体方案和离线实现。今天不启动这些方向，当前结果不构成 v0.0.8 发布验收。
