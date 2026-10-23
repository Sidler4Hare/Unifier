# Unifier 当前接手入口

## 定档：Unifier 架构实验基线（2026-10-23）

- 项目统一更名为 **Unifier**，私有仓库 `Sidler4Hare/Unifier`，定档标签 `architecture-experiment-2026-10-23`。本节覆盖下方历史阶段的“未提交”状态描述。
- 以 [定档说明](设计/Unifier-架构实验定档-2026-10-23.md) 为当前架构与验收范围入口；历史报告、失败尝试和冻结源码保留原文。
- 新命令入口 `tools/unifier.py` 兼容委托到 `tools/ml.py`；内部 Python 包、环境标签、MIME 和已注册应用不迁移。
- 当前机器使用 `/home/omg/文档/Unifier` 作为新目录入口；它指向原物理工作区，维持路径派生的环境身份与历史证据引用。克隆到其他目录可继续开发，但旧运行记录不能直接迁移。
- 本轮完整工作区备份放在相邻的 `Unifier-backups/`，包含未跟踪、忽略文件及 Git 历史，详见其中恢复说明；不把临时套接字当作可恢复文件。
- 后续围绕这份基线实现文件控制层，不扩大历史实验的通过范围。

## 历史：两个契约修复及 FreeCAD 真 GUI 验收（2026-10-23）

- 报告：`evidence/contract-freecad-gui-2026-10-23/report.md`；修复前四文件保存在 `before/`，本轮独立差分为 `contract-fix.patch`。没有提交。
- 创建、计划和驱动共用 `resolve_target_contract`；持久记录经 `resolve_record_contract` 校验。未知 apt 目标提前拒绝；已知目标的矛盾 system 拒绝；只有缺少 system 的旧记录可用旧字段兼容。未实现政策实际操作均 refused，不能把可描述当可运行。
- 回归 162 项，158 通过、4 跳过。新增 6 项测试涵盖多个契约字段和四种包操作，并对三套旧记录做模拟事务，核对计划与执行驱动一致。
- 使用已有本地镜像创建独立 `gui-freecad`，无 pull/包安装，network none。真 Wayland FreeCAD 1.1.4 GUI 经过 QAction 和真实 Open/Save 对话框完成：宿主原件打开、加立方体、原地保存、关闭重开、新建模型、仅输入文件名保存到当前宿主项目、正常退出自动停止、再次打开两份文件。
- 停机后回读原件 4 对象、新件 1 对象，形状有效，哈希稳定，UID/GID 1000:1000；最终 stopped、无租约/bridge/窗口，原有环境和旧模型证据保留。
- `attempt1-qt-import/`、`attempt2-dialog-submit/` 保留探针失败。最终 41 项证据检查通过；未把它们算成 41 个独立 GUI 场景。
- 新建默认目录仅验收本次先打开宿主文件再新建的流程。通用文件路由、原文件管理器集成、任意空白启动的保存默认值和并发防覆盖仍未实现。旧架构 v2 与上一节报告中的两个缺口现在以本轮修复报告为准。

## 最新：架构复核与文件控制层 v2（2026-10-23）

- [改动与测试复核](调研/Grok改动复核与架构迭代-2026-10-23.md)、[设计 v2](设计/文件与执行控制层-v2.md)、证据 `evidence/architecture-review-2026-10-23/`。
- 对比当日 09:30 源码哈希：新增 catalog，修改 routing/packages/service/cli。包驱动与更换计划已部分落地；文件层仍是设计。不要把 10 月 22 日的全部未提交改动归入此次增量。
- 本轮当前工作树回归 156 项：152 通过、4 跳过。新增 13 项文件机制检查通过；三处进程中断后重复恢复保留源，无重复登记。仅普通文件的复制发布/保守恢复，不是源删除、断电恢复或产品移动验收。
- 两项待修契约边界：新 apt 目标可通过 require_runnable，但 driver_for 只接 PREPARED；合成矛盾 system/target 记录会让计划与实际驱动不一致。未改真实环境记录。
- 下一轮按 v2 关口实施：统一目标契约与身份；宿主项目调用 Arch 应用保存；停止环境只读投影；随后补移动源清理、真实故障矩阵。必须验证宿主跟随投影绝对链接，不能只在服务端 chroot 测试。
- 本轮未改生产代码/测试/配置/工具，未重跑旧 GUI 或启停容器，未提交。原有脏工作与历史证据保留。下面各节为历史阶段，不是当前最新架构状态。

2026-10-22，本轮开发分支：`codex/screencast-protocol`，基线 `0c14564`。

用户已确认腾讯会议应用轮实测通过。本轮继续共享桌面前端、GPU DMA-BUF 与 CPU MemFd 的协议连接。M5 跨宿主尚未开始。

2026-10-22 深夜，本轮先完成生命周期与安装状态准确性修复，再做并发测试。生产代码在工作树中未提交。

## 最新一轮：生命周期与安装状态修复（2026-10-22 深夜）

用户要求先做修复与验证，顺延并发测试。两项修复均已在真机验证：

- 生命周期：`--init`（PID 1=catatonit）、受管启动器的 detached worker 租约、auto_stop 按 container_id+started_at 继承、start/stop 的 lifecycle flock 与 stopping 等待、桥接健康重建。
- 真机第二轮 `evidence/lifecycle-2026-10-22T213032+0800/`：`product_verdict=lifecycle-fixed`；20/20 冷启动通过，关窗→died p50 199 ms、记录 stopped p50 24.5 ms，快速重开 p50 884 ms，Debian/Arch 各 30 次热组后 0 僵尸，SIGKILL 启动器、并发双窗口、手动保留、3 次并发首启全部通过。
- 第一轮 `evidence/lifecycle-2026-10-22T212236+0800/` 保留 2 次 Debian 关窗测量超时作为原始失败记录；根因是共享桌面真实键盘输入触发 Mousepad 自身 Save Changes 模态框，不是产品停止流程缺陷。第二轮改用只读文档并加临时 KWin 焦点保护，同冻结代码完整重跑通过，最终判定以第二轮为准。
- 安装状态：事务新增 `maintenance`（status/items/system_integration/full_init）与 `verdict`；`pkg log` 输出 verdict，CLI 对 `verified-with-warnings` 打 stderr 警告且仍返回 0。真实 Arch 3 个事务通过：正常包 verified；合成失败钩子与真实 Sunshine 均为 verified-with-warnings（Sunshine 5 类维护失败）。证据 `evidence/install-status-2026-10-22T2108+0800/`。
- 全量回归 149 项通过（4 项 `MULTILINUX_INTEGRATION` 按设计跳过）。所有测试环境停止保留；无存活租约/桥接/窗口；用户原有腾讯会议容器保持运行。
- 代码改动：`src/multilinux/{runtime,on_demand,service,cli,packages}.py`，新增 `tests/test_lifecycle_contracts.py`、`tests/test_install_status.py`、`tools/{prepare,probe}_lifecycle.py`、`tools/{prepare,probe}_install_status.py`。
- 并发测试已于 2026-10-22 晚完成（task-4）：`evidence/concurrency-2026-10-22T220318+0800/`，`product_verdict=concurrency-verified`，44/44 设计波次通过。共享 Debian 环境 3 窗口：单容器、单 start、create=0、lease=3、进程=3；双发行版 Debian 3+Arch 3 运行中容器恒为 2；母发行版原生 3 Konsole 不产生容器；混合 2+2+2 容器=2；10 wave ×3 并发首启 stop p50 213 ms、停止中重开就绪 p50 1942 ms。S5/S7 各有一轮测量口径失败，原始记录在 `attempt1/`、`attempt2/`，修正后重跑通过，不是产品缺陷。
- 下一轮候选：Fedora 容器并发、更大 N/长时间压力、网络与 I/O 并发、M5 跨宿主；不要把本轮 `concurrency-verified` 扩展成 M5 或所有应用的结论。不要覆盖上述生命周期/安装状态/并发证据。

## 前一轮：复杂 Arch 应用（2026-10-22 20:23 后）

- 最新报告：`evidence/arch-complex-2026-10-22T202359+0800/report.md`。FreeCAD 1.1.4 原生 Wayland 的参数建模、中文文件、STEP 回读和停止后重开通过，最终真实窗口已检查，两次启动器正常退出。
- GitHub Release 的 Sunshine 原生 Arch 包完成哈希核对、本地安装和普通用户版本查询。但 udev/modprobe/systemd 钩子失败，pacman 仍返回 0，项目记 `completed`；这不是完整兼容通过。设备与串流未验收。
- 默认镜像两次下载失败，独立环境增加 USTC 镜像后恢复；保留所有失败和修正前 GUI 探针尝试。
- 独立环境 `complex-arch` 已停止并保留；宿主包、桌面入口、原项目状态核对不变。生产代码未改，已有生命周期缺陷仍在。
- 后续测试优先级见 `调研/复杂Arch应用与后续测试.md`。新增工具为 `tools/probe_arch_complex.py` 与 `tools/probe_freecad_macro.py`。

## 前一轮：启动性能与回收（2026-10-22 晚）

用户随后要求开始测性能。最新证据是 `evidence/startup-2026-10-22T182400+0800/report.md`，不是前面的 ScreenCast 功能报告。

- Debian/Fedora/Arch 的原生 Mousepad 冷、热启动各 30 次，共 180 个正式样本。冷启动 p50 约 0.871/0.969/0.891 秒，热启动约 0.592/0.672/0.586 秒；采用窗口加实际 buffer commit 的界面就绪口径，未测物理呈现。
- 正常并发租约通过，但完整回收约 8.36 秒，快速重开约 8.94–9.10 秒；PID 1 sleep 没有信号处理器，停止事件退出码 137。
- Arch 31 次连续热组打开后累计 155 个僵尸子进程。已随本轮环境停止清除。
- 三套环境强杀启动器后都丢失自动回收所有权，下一次正常受管启动也不会恢复 auto_stop。
- 机器证据有效，产品结论为 `needs-lifecycle-fixes`；105 项代码回归中 101 通过、4 跳过，不代表上述实际缺陷已经修好。
- 本轮只修改测量工具，没有修改 `src/multilinux/`。下一步先修 PID 1 的信号与回收职责、异常租约所有权，再处理重复 Podman 调用，以相同场景复验。
- 新测试环境、代理、窗口和临时 KWin 脚本均已回收；镜像和停止的实验环境保留用于复现。原腾讯会议及原始 GUI 环境保持原状态。

本轮 DSH 通道：`session-a36617c6-89f3-4e8d-82ce-8981dc67b5dd`，仍为 Command Code 的 `deepseek/deepseek-v4.1-flash`。启动器和两份任务交接在 `.lab/agents/startup-20261022T182400/`，可提交副本位于新证据目录的 `agents/`。

离线复核：`python3 tools/verify_startup_evidence.py --run-dir evidence/startup-2026-10-22T182400+0800`。全新重测使用 `tools/prepare_startup.py` 创建新证据目录；不要覆写本轮样本。旧 ScreenCast 的哈希清单对应 `8482968` 时的文件，后续 README/HANDOFF 更新属于文档漂移。

## 已有修复和部署

- Gemini 修复记录：`/home/omg/.gemini/antigravity/brain/f1790762-eb63-403a-b01c-1864b54fd49e/walkthrough.md`。
- 已验收的腾讯会议部署：`/home/omg/.local/share/multilinux/apps/wemeet/`；启动配置和新增 PipeWire 包事务在该目录的 `.lab/state/`。
- 原始摄像头失败证据是修复前记录，应保留作历史诊断。
- Grok 会话 `01a16174-992e-7cf3-9b3c-97dc91ea6f23` 完成合并和 `0c14564` 的现场证据提交。

## 本轮 DSH 子代理通道

用户指定使用 Command Code 套餐的 `command-code / deepseek/deepseek-v4.1-flash`。
启动器和调用级配置在 `.lab/agents/screencast-20261022T155040/`，不修改全局模型选择。

| 角色 | 持久会话 | 独占文件 |
|---|---|---|
| portal | `session-5c4dc75e-c116-4624-b564-7b0bf29729c4` | `src/multilinux/screencast.py`、`tools/probe_screencast.py`、`tests/test_screencast.py` |
| native | `session-15edc679-945b-4ffa-b552-fb3f7931dbb7` | `fixtures/screencast_consumer.c`、`tests/test_screencast_native.py` |
| gpu | `session-4a0b93b9-c8df-4975-9aac-21fa7ff4a924` | `fixtures/dmabuf_roundtrip.c`、`tests/test_dmabuf_roundtrip.py` |

每个角色的 `*-run.json` 记录进程、状态和会话 ID，`*-handoff.md` 记录完成后的交接。
代理退出后，可用启动器的 `--session-id` 和 `--prompt-file` 继续该代理。启动器只在进程环境中加载本机 Command Code 凭据；不要打印或提交凭据及原始推理日志。

## 本轮现场环境和复现

独立证据目录：`evidence/screencast-2026-10-22T155700+0800/`。
实验项目在其 `project/`，共享输入输出在其 `work/`，环境 ID 为 `screen-debian13`。
原腾讯会议及其登录状态保留；本轮编译依赖安装到独立 Debian 环境。

入口：

```bash
python3 tools/probe_desktop_protocols.py build --run-dir evidence/screencast-2026-10-22T155700+0800
python3 tools/probe_desktop_protocols.py capture --run-dir evidence/screencast-2026-10-22T155700+0800 --mode memfd
python3 tools/probe_desktop_protocols.py capture --run-dir evidence/screencast-2026-10-22T155700+0800 --mode dmabuf
```

抓帧通过系统 Portal 选择名为 `Multilinux Screen Share Test` 的合成窗口，记录实际缓冲类型、帧变化和色块统计，不保存屏幕图像字节。GPU 自检使用自己创建的 GBM 色块缓冲，独立于屏幕抓取。

## 当前完成状态（2026-10-22 18:01 +08:00 后）

- 三个 DSH 代理均已完成。交接报告的可提交副本在证据目录的 `agents/`；持久会话和本机启动器仍可接续。
- 真实系统窗口选择 → CreateSession/SelectSources/Start → OpenPipeWireRemote → Debian 原生消费端完整通过，经过现有过滤 D-Bus 代理，没有放宽其访问规则。
- MemFd 实际取得 60 帧 BGRx，色块通过，哈希变化 57 次。最终源码的 auto 回退复测另取得 60 帧 MemFd，哈希变化 48 次。
- DMA-BUF 实际取得 60 帧 BGRx，900×598，linear modifier，EGL/GLES 导入成功，哈希变化 56 次。使用 `glReadPixels` 核验，明确 `zero_copy=false`。
- auto 故意使用不存在的 render node，先记录 GPU 失败，再关闭原 FD、重新取得 Portal FD 并通过 MemFd，不把 CPU 成功记作 GPU 成功。
- 主机与 Debian 的私有 GBM 缓冲自检均为 7747/7747 RGB 像素匹配。
- 102 项代码回归，98 项通过，4 项旧现场测试按原规则跳过。新窗口采集由独立现场工具实际完成。
- 实验环境已停止，应用租约为空、桥接代理已撤销、合成窗口退出、Portal session/FD 均关闭。宿主包和桌面入口清单前后相同。

集成时修复了三个实际问题：Gio 的 `a{sv}` 参数必须正确构造外层 tuple；长中文项目路径下代理 socket 必须相对私有目录绑定；Debian 需要 `pipewire-bin` 提供 `client.conf`，仅有开发库和 modules 不够。构建阶段现在先检查客户端运行文件和 context 创建，再允许弹出共享框。

DMA-BUF 按 PipeWire 1.4 协议处理：EGL 导入不把 `maxsize`/`chunk.size` 当作线性内存边界，允许这些字段为 0；modifier 使用 mandatory/don't-fixate 协商，过滤 external-only 与不能证明为单 plane 的组合。MemFd 仍严格检查 offset/stride/chunk。

失败记录原样保留：首次准备因共享父目录被拒绝；前两次现场捕获因 Debian 缺少 `client.conf` 而失败。后续记录明确区分失败和复测通过。

## 后续边界

本轮只验收 Fedora 44 KDE/KOS 宿主上的 Debian 13 消费端、合成窗口和 single-plane packed RGB。实际 negotiated modifier 为 linear；非线性 modifier、多 plane、其它 GPU、monitor 选择、Portal v6 serial、长时间压力与跨宿主 M5 不应按本轮结果宣称通过。Portal v6 目前只有注入测试。

上层长期预览/编码界面仍可在这条已验证的连接上继续实现。原腾讯会议部署与其用户验收结果保留，不重开旧摄像头任务。

最终进度应以本轮 `report.md`、`validation.json`、源码哈希和实际捕获结果为准。编译、离线自检、Portal 会话握手、实际取帧、GPU 导入和跨宿主验证分别记录。
