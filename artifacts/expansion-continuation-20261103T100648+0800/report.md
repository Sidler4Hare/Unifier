# 扩大测试续测与桌面防护（2026-11-03）

本轮仍为 **PARTIAL**，没有把整套扩大测试宣告完成。已完成控制 FD 兼容修复、受控请求桥及重启后的会话恢复修复，并独立验证了原生包事务和真实客体请求。桌面事故后使用独立显示和资源上限，不再操作用户 KWin。远端长测、解释器回归与本机应用数据工作流已收齐，以下分别说明已通过部分和仍未完成的合同。

代码仅在工作树，版本仍为 0.0.2；没有提交、推送、重建发布包或更新日常部署。原 9 个容器保留且停止，两个日常容器配置和状态、Git 暂存条目保持一致。[保全审计](preservation-audit.json) 的范围是实例、配置和状态；本轮没有新做日常文件全树的前后摘要，不能据此宣称全部日常文件逐字节不变。

本轮 17 个源码、用例及说明文件的工作树增量见 [source.patch](source.patch)，对应摘要见 [文件清单](validation/final-worktree-files.json)。补丁不含测试账号或 dsh 凭据；这不是发布包。

## 桌面事故与本轮保护

旧轮次观察到 KWin/会话失稳和宿主重启，根因 **UNKNOWN**；没有证据可以直接认定 OOM。事故前测试未使用汇总资源上限。事实在 [事故记录](desktop-incident/result.json)。事故后的启动身份 `6bac6ae6-c583-48b1-af8e-fc533c23407b`、KWin PID `1769` 在本轮受保护测试后保持一致。

- 本机汇总测试 slice：内存硬上限 1 GiB、swap 128 MiB、256 个任务、1 CPU；每个新 GUI fixture 为 768 MiB、128 MiB swap、192 个任务、private IPC，创建时禁止宿主显示/音频/DBus/设备挂接。
- 控制器自身进入 slice；`GOMAXPROCS=2`，通常 96 个任务。旧 Podman 在过紧的控制器额度中曾出现 Go 线程创建失败，保留失败实机回执，不把它当作容器已停止的证据。
- 创建/启动至少保留宿主 2 GiB 加客体预算。运行中连续三次低于 2 GiB，只定向停止重新核对过的测试 CID；指标失败也中止。实际发生过一次 [内存预留触发中止](private-gui-workflows-v2/reserve-abort.json)，桌面身份未变。
- 新保护实现写入持久中止锁；新工作进程也不能重启已中止 fixture。[实机反例](desktop-incident/app-interlock-result.json) 确认新 app 工作进程返回拒绝且容器启动时间不变。前置步骤失败后，新的驱动立即放弃后续命令。
- 旧私有 GUI 驱动在第二次重开被拒绝后仍尝试后续打开；硬 cgroup 限额始终存在，但软门槛链路不完整。该失败原件保留。新的 app CLI 测试入口为工作进程也注入保护运行时，不再依赖仅外层检查。
- 独立 32 MiB cgroup 的故意分配超额测试被局部 OOM 终止，宿主 boot/KWin 不变，见 [限额实证](desktop-incident/resource-containment-proof.json)。这不构成“绝不会崩溃”的保证。

保护适配器是扩测工具，不改变日常产品默认运行方式。说明和恢复约束见 [扩测保护与恢复](../../docs/扩测保护与恢复.md)。

## 修复及实机证据

| 项目 | 本轮结果与边界 |
| --- | --- |
| 控制管道 | 用单独子进程把控制管道映射到 FD 3，传 `--preserve-fds 1`；父进程不 dup2、不使用 preexec_fn。解决 Ubuntu/runc 只支持复数参数而无法开始事务的问题；真实 crun/APT 和 Ubuntu/runc/Fedora DNF 均有回执。 |
| 包事务 | [Fedora DNF](remote/dnf-v3/result.json)：jq 安装、删除、重装以及 `libasound.so.2` 唯一仓库提供者 alsa-lib 的自动修复均 completed，最终 verification.ok=true；先前失败环境保留，其他原有容器不变。不能代替所有厂商包更新/卸载。 |
| 受控请求 | [六单元实机](remote/verified-final/bridge-six-cells-v10-evidence/result.json)：Ubuntu 上 Fedora、Arch 两种来源各请求 host-info、自身项目、另一客体项目；真实 guest PID namespace、发行版、目标 CID、宿主文件 UID 和长 socket 路径有回执。另有 6 个实际客体拒绝用例（重放、未知动作、坏 token），4 个同一运行实例内的服务端撤销校验。不是客体原生模型工具调用验收。 |
| 请求边界 | 必填来源 binding、guest/lease 存活、绝对授权期限；固定目标 UUID/CID/argv/cwd/scope 重验；创建会话后复核 scope；持久 UUID 去重；token 递归脱敏；过大撤销表拒绝而非截断；非零退出保留码并返回失败；未收尾线程或同一来源的持久 claimed/unresolved 回执使 broker 拒绝后续执行，重启不能抹掉该阻断。没有通用宿主 shell。 |
| 长 socket | 每级目录不跟随符号链接；通过目录 FD bind/connect，bind/chmod/清理保持锚定，目录路径被替换时不操作替代位置。离线反例和真实客体长路径都已验证。 |
| 凭据传递 | 项目会话使用 `--env KEY` 加宿主子进程私有环境，不把授权值放入 helper/Podman argv，不改父进程环境。两种真实来源都检查了实际宿主 proxy argv；报告只保留布尔结论。 |
| 停止实例恢复 | 复核原 UUID/generation/CID/标签后，精确同一停止实例可证明 guest 已结束；无法 inspect、替换实例或外来标签保持未知，运行探测和直接取消固定 CID。另修复相对安装根应按规范项目标签核对的问题。 |
| 中断数据恢复 | [真实两条旧会话](soak/bounded-recovery-v2.json) 已变 interrupted 并释放租约；未重启旧 GUI。两份 SQLite integrity_check=ok，各 84 行。旧 30 分钟长测仍为中断，不改写成通过。 |

新命令和权限语义见 [客体受控请求](../../docs/客体受控请求.md)。本机 dsh 的 `Local qwen server`、模型 ID `swift-1.5-iq3_s`（配置显示 qwen 3.8 flash）、max 负责候选实现，主控独立修补与验收。[执行记录](qwen-execution.json) 区分正常完成的 runtime 任务和到时限停止的 bridge review；模型结束本身不算验收。没有调用内置子代理，也没有把凭据文件纳入证据。

## 应用、文件与媒体

这部分使用停止的原 Debian 安装快照建立独立 fixture，复用既有厂商安装；不能替代新安装、升级、卸载或三发行版九单元验收。共享目录只有合成文件，输入、截图和窗口关闭事件均在客体 Xvfb 内；预启动私有显示也不代表桌面应用自动停机合同已验收。

| 工作流 | 已取得的实际证据 | 仍有限制 |
| --- | --- | --- |
| [Writer](private-gui-workflows-v4/writer-result.json) | 两轮 GUI 编辑保存，完成 **2 个停机重开循环**，重开界面和 DOCX 均保留标记；含空格和字面 `$;` 的另存成功；三次正常关闭返回 0。 | 模板字体缺失、字体替代与版式一致性未通过；第二轮曾被资源门槛拒绝，后续在保护入口下补齐，失败原件保留。 |
| [Sheet](private-gui-workflows-v6/sheet-result.json) | 两轮编辑及 **2 个停机重开循环**；公式与缓存合计为 29、35，新增公式结果 211、322；中文单元格保留。另存 `sheet copy $;.xlsx` 后再次停机重开，四次正常关闭返回 0。 | 原聚合模式显示崩溃提示，pids.max 计数 5、内存 OOM 为 0。仅测试副本备份配置后改 `prome_independ`，后续同限额下 pids.max/OOM 均为 0；不能据此把聚合模式宣告通过或断定唯一根因。 |
| [演示](private-gui-workflows-v7/slides-result.json) | 两轮 GUI 编辑保存与 **2 个停机重开循环**；标题、中文及原正文和两个标记均由 PPTX 核对。另存 `slides copy $;.pptx` 后再次重开可见。 | 三次正常关闭返回 0；副本最后的关闭尚未观察完成就定向停容器，返回 137，未算正常关闭。v6 首次启动时显示抓取失败，根因未知，后续 v7 成功不抹除失败。 |
| [Beyond Compare](private-gui-workflows-v8/compare-result.json) | 两个含中文、空格、字面 `$;` 的文件：选择一处右→左合并，保存、关闭、停容器重启并重开；另一处差异和中文保留，右文件 SHA256 不变。两次均用客体 `WM_DELETE_WINDOW` 关闭并保留原生码 13。 | 原生码 13 表示仍有规则差异，与刻意保留的一处差异一致，见 [厂商说明](https://www.scootersoftware.com/v5help/command_line_reference.html)。私有 Xvfb 下 Session 菜单呈黑色，菜单视觉未通过；未把数据工作流当成全部界面通过。 |

Writer PDF 导出为 1 页，提取文本含两轮标记和中文，已渲染查看；字体保真仍未接受。截图只来自合成文件，未公开用户桌面、账号或二维码。最新 [GUI 启动门槛核验](desktop-incident/final-guard-preflight-v2.json) 已拒绝新增启动：采样可用内存 1,786,978,304 字节，低于 2 GiB 宿主预留加 768 MiB 客体预算；测试容器保持停止。本机 30 分钟 GUI 长测因此尚未补跑。

[文件系统 R2](filesystems-r2/result.json) 是 24 个有界普通文件检查：Btrfs、tmpfs、userspace fuse2fs/ext4 的 6 个有向复制方向、受控移动/冲突及安全拒绝。ext4-fuse 源清理因缺少 RENAME_NOREPLACE 明确保留为 SOURCE_CLEANUP_PENDING，源和目标均保留；这是预期安全拒绝，不能称移动完成。[ENOSPC](enospc/result.json) 在专用小镜像中失败且不发布目标，源摘要不变，随后小文件复制恢复。不是内核 ext4 挂载、客体 bind、递归树或 ACL/xattr 保真验收。

[飞书实测](media-attempt1/result.json) 保留用户确认：双向声音正常，电脑可见手机连续动态视频。共享 WPS 时会议被“检测到安全问题”终止；账号风控、同账号限制和 portal 原因未区分。摄像头发送未测，设备未挂载。用户最新确认目前只能使用同一账号；没有重复触发该安全终止或绕过限制，也没有把用户推测写成根因。

## 远端 CLI 与解释器

[新增 Fedora 六款 CLI](remote/final-additions/fedora-cli-evidence/result.json) 在独立环境完成原生安装和 15 次命令回放：jq、ripgrep、fd、bat、SQLite、htop；包含中文/空格/字面符号、原生退出码、SQLite 关停重开。htop 在真实 PTY 绘制并按 q 正常结束；[独立尺寸复测](remote/final-additions/fedora-pty-resize-evidence/result.json) 从客体 stty 读到 31×103 → 37×111，结束后 guest gone、租约释放。此前 Debian/Arch 的 12 个核心单元仍按旧回执引用，不能把本次 6 个单元当作三发行版全部新跑。

本机 Python 3.14 [最终全仓检查](validation/final-offline-v4.log)：**Ran 726，skipped=6，无失败/错误**。原生设备入口显式设置为不存在的 render node，避免离线检查意外使用宿主 GPU；这批跳过没有算图形设备通过。该日志之后只调整测试驱动、报告与文档，产品代码及用例未再变化。

Python 3.11.18 使用校验过的 [python-build-standalone 20261026](https://github.com/astral-sh/python-build-standalone/releases/tag/20261026) GNU 运行时，隔离于远端测试目录，系统 Python 未变。首份源码包遗漏 C fixture，保留为 core-only；最终源码已补齐。最终 [完整回归](remote/verified-final/python311-verified-evidence/check-repo.log) **Ran 713，skipped=7，无失败/错误**。其中两条是类级 setup 跳过，因远端缺 PipeWire/GBM/EGL/GLES/libdrm 开发依赖，影响 13 个原生用例；类级跳过不计入 Ran，不能用 713−7 计算通过数。其余 5 条是逐用例条件跳过。两边收集同一 726 项，运行时、测试、C fixture 逐文件摘要一致；回归后桥接说明文档有改动，详见 [源码比对](validation/final-source-match.json)。

## 持久性与收尾

[受限远端长测](remote/final-additions/guarded-soak-evidence/result.json) 实际持续 **1804.35 秒**：Fedora 与 Arch 各 120 轮、每 15 秒保存，私有 SQLite 各 120 行，共享协调数据库 240 行。独立逐行 SHA256、完整计数、进度和 integrity_check 均核对；自然退出 0、租约释放、工作线程为 0、两个客体停止，宿主 boot 未变。期间同一总限额下还有 CLI/解释器工作，因此这是并发负载下的 headless 功能持久性结果，不能当作隔离性能 SLO、本机 GUI、账号媒体或断电证明；原本机中断长测仍保留 interrupted。

[远端最终审计](remote/verified-final/final-audit-v2-evidence/result.json) 再次核对 480 条数据库记录的内容与计数，五个本轮测试容器均停止；本机原 9 个容器保全见开头回执。独立测试镜像和私有 profile 仅留在测试目录，未作为发布物导出。可复查的 [受控 GUI 驱动](protection-drivers/) 不包含私有凭据。

受控请求桥仍为原型：没有自动请求日志恢复命令，也没有 broker 专属 SIGTERM/SIGHUP 优雅收尾保证；硬退出可能留下 guest/租约和持久阻断，须核查并恢复真实项目会话。不能通过删请求日志跳过未知执行，不能把持久阻断测试当成自动故障接管。

## 尚未完成

完整台账在 [remaining.json](remaining.json)。包括字体/版式、比较工具菜单和演示副本正常关闭、九个厂商安装生命周期单元、真实宿主桌面及媒体、客体原生 dsh/模型工具链、其他宿主的请求桥矩阵、宿主 provider/维护动作、watcher、通用 IDE/GPU、ACL/xattr/递归树和严格快照/多卷。安全拒绝、一个窗口和 Python 客户端不能替代这些结果。

本机 GUI 后续只在足够资源和完整保护链下恢复；需要账号、设备或其他宿主桌面的部分仍待对应条件。新增原型能力及未实现合同单列，不能在总体验收中隐去。
