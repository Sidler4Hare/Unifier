# Unifier v0.0.3 当前接手入口

现行包是 `src/unifier`，用户入口是 `unifier`，诊断入口是 `python3 tools/unifier.py`。`src/multilinux` 和 `tools/ml.py` 只做旧名字转发。证据在 `archive/evidence`，根目录 `evidence` 是符号链接。已完成的阶段稿和早期调研在 [archive/notes](archive/notes/README.md)。布局见 [docs/布局.md](docs/布局.md)。

分支是 `codex/unifier-v2-control`。产品行为以仓库根 [README.md](README.md) 和 `设计/` 里留下的六份说明为准。

## 接手时先记住

- 2026-11-07 维护：日常入口已切到当前树重新组装的 v0.0.3，目录是 `~/.local/share/unifier-code/releases/0.0.3-892b1623c220`，`unifier --version` 为 0.0.3。源码包 `artifacts/unifier-0.0.3.tar.gz` 的 SHA-256 是 `892b1623c220311d9cd3b770cfcf4236070b8d0fc7bd81f3822d27c20616a032`。11 月 5 日的旧包字节在 `artifacts/backup/unifier-0.0.3-20261105/`。已安装的 0.0.1 和两份 0.0.2 代码目录移到 `~/.local/share/unifier-code/backup/`，回退链接是 `~/.local/bin/.unifier-0.0.2.rollback`。FreeCAD 与腾讯会议的数据根没动，只把它们的启动包装改到这一版代码。用户数据 `~/.local/share/unifier` 保留。这次切换不是新的实机验收。

- 2026-11-06 续测见 [本轮回执](artifacts/expansion-resume-20261106T095650+0800/report.md)。原固定 Debian CID 的最小启动/执行/停止已通过，并取得 init、conmon、pasta 和控制器的汇总 cgroup 证据；完整共享负载仍未通过。真实 APP 路径发现并修复默认空参数被拒绝、客体 HOME 覆盖宿主 Podman 环境两处问题；通过匿名 env-file 传递客体环境，保持宿主客户端配置与凭据 argv 边界。多应用复测被 PSI 保护中止，随后停止态包库探针被控制器子组回收保护提前中止，原始失败/中止锁保留。固定 CID 已停止，正式 app recover 已释放中止执行的租约。当前控制器 192 MiB/64 任务不足，扩大子预算尚未执行；不要把最小样本当作全部 PRE03/SH/DEP 放行。收尾审计 `final-audit.json` 仍是 FAIL，只有索引文件哈希不同；索引树与 HEAD 树一致，说明见该回执的 `index-clarification.json`。END01 操作收尾已记录。2026-11-06 已授权并执行一次 384 MiB 控制器的停止态包库复试，容器启动前再次因回收压力中止，见 `inventory-pilot-384/`。同一保护类不再自动复试。v0.0.3 现有源码包和日常 v0.0.2 安装版均未重建或部署这批修复。

- v0.0.3 组装：已将本分支现有增量纳入新版源码范围，并增加日志驱动的 `pkg repair-deps --from-log`；`--diagnose-only` 不访问产品环境。新版包、离线验收和原包保全以 [组装报告](artifacts/release-v0.0.3-20261105T152926+0800/report.md) 为准，日常安装版未自动升级。下面带日期的回执描述各轮结束时的状态；“未提交/未构建”和旧测试数量不能覆盖本轮状态。PRE03、同管理根真实分支、目标加载器及 GUI 仍独立阻断。

- 2026-11-05 当前状态：先读 [全项目测试禁令与规范](docs/测试禁令与规范.md) 和根 [AGENTS.md](AGENTS.md)。默认不新建项目容器或管理根，不按名称杀进程，不操作用户桌面。实机须有整棵进程树硬预算、独立监测和期限。现行宿主保留线是 **1 GiB**。`tools/guarded_test.py` 仍是旧适配器：同进程线程、每秒采样、连续三次低于它自己的 2 GiB `HOST_RESERVE` 才中止，而且 `create()` 会新建容器。它不能当作 T2 放行器，旧集成用例也不能直接启用。
- 同日架构修复停在 **PARTIAL**，源码在本分支工作树。S1 管理根协调锁、S2 停止态 Debian 只读清单、S3 APP subreaper 租约与恢复、S4 bundled 候选分层，各有限定 T1 回执：[首轮](artifacts/architecture-repair-20261105T104613+0800/report.md)、[续轮](artifacts/architecture-repair-20261105T111850+0800/report.md)。运行中的同伴、Arch/pacman 和 RPM 清单明确 unsupported。存储引用释放失败会持续阻断，没有自动恢复命令。通用单实例和外部服务代启未实现。同管理根真实同伴 FIX01-05 与实机保护 PRE03 为 BLOCKED。发布包和日常安装版没有重建或部署。11 月 3 日的 726 项全仓检查早于这批改动。下一步是当前树的有界全量离线检查，然后 PRE03，再是一台既有 Debian CID 的最小准入。
- 2026-11-03 续测按用户指定的 dsh 本地 Qwen 实施候选补丁，主控独立验收。新增可移植 FD 3 控制管道、凭据不进 argv 的项目会话传递、精确停止 CID 恢复和受控请求桥原型。Writer/Sheet/演示各两轮私有 GUI 保存/停机重开、Compare 选择性合并/保存/重开通过数据检查；Ubuntu 上 Fedora/Arch 六个请求组合、受限 1804 秒持久性、726 项本机回归与 Python 3.11 回归已收齐，数字属于当时的树。总体验收仍 PARTIAL。原型没有请求日志自动恢复命令，broker 也没有专属 SIGTERM/SIGHUP 收尾。见 [续测报告](artifacts/expansion-continuation-20261103T100648+0800/report.md)。这批源码随后留在本分支；日常已安装的 v0.0.2 发布物不包含它。
- 同轮发生过宿主桌面事故，根因 UNKNOWN。后续禁止重复使用旧 KWin 或宿主桌面自动化；先读 [扩测保护与恢复](docs/扩测保护与恢复.md)。那一轮适配器的门槛是宿主 2 GiB 再加客体预算，最终启动核验因低可用内存拒绝，测试容器保持停止。不得删除那次中止锁，也不得改用普通 app worker 绕过。原容器和暂存条目的备份、精确 CID 见该报告。事故记录里的 2 GiB 是当时适配器的阈值，现行项目保留线以测试禁令的 1 GiB 为准。

- 2026-11-01 续修：用户指定通过 dsh 的 `Local qwen server` / `swift-1.5-iq3_s` / `max` 负责编码。已独立验收包文件路径保真、实机默认回执保全两项本地模型补丁；另复核保留包 CID、递归补库和进程组清理修复，更新宿主准入与隔离集成测试。本轮 633 项离线检查（628 通过、5 条件跳过）及 Debian/Arch 真实有界探针通过，9 个原容器状态不变。该轮结束时只有工作树、尚未部署。身份、备份、补丁和边界见 [续修回执](artifacts/continued-repair-20261101T154240+0800/report.md)。

- 2026-11-01：v0.0.2 补齐包事务执行前日志、SIGTERM/HUP/INT、跨会话子进程树清理、超时后的清单重核验和 `pkg recover`。不明状态或原生数据库未修好会挡住下一次包变更。回执、精确构建身份、部署和回退入口见 [本轮报告](artifacts/expansion-fix-20261101/report.md)。原生中断测试用独立 `ml-26942dd5-interrupt-debian`，不属于日常环境；未检查精确 CID 前不要删除或复用。

- 2026-10-30 宿主关系策略版本 2：禁止同派系容器，包含 Ubuntu → Debian。正向测试必须过滤宿主派系，旧同派系执行记录不算设计通过；已有环境保留数据，允许查看/停止/删除，不能继续启动或执行。
- 同日补齐运行态 `ls` 检查及服务层每安装根 target 唯一性和跨进程创建锁，已承接的 target 也不能绕过；日常部署做最小准入补丁。`compat matrix` 有任一准入目标返回 0，具体目标仍用 `compat check` 判断。新版交付与验证见 `artifacts/host-authority-fix-20261030/report.md`，不改旧回执。

- 日常容器只有两个：FreeCAD `ml-53c5c8a5-freecad-arch`，腾讯会议 `ml-6b061a2f-meeting`。2026-10-31 本机扩测另保留两个已停止的 Debian/Arch 测试容器，安装状态供剩余阶段续用；这不是最终拆除。身份与恢复入口见 [本轮报告](artifacts/extension-local-20261031T111237+0800/report.md)，后续按精确身份清理。
- 容器标签、容器名、MIME、桌面 ID、容器内路径、`.ml-` 前缀、投影 inode 盐和 `MULTILINUX_*` 标记保持原拼写。探针线协议 `multilinux.*.v1` 也保持原样。
- `archive/evidence` 的字节保持原样。报告里的旧路径是归档时的路径。
- 物理工作区是 `/home/omg/文档/多兼容linux机`。`/home/omg/文档/Unifier` 指向它。路径参与环境身份，搬动物理目录会改变身份。
- 快捷命令测试按英文片段断言。本机 locale 是中文时，用 `LC_ALL=C LANG=C` 跑 `tests/test_cli_shortcuts.py`。
- 统一离线入口是 `python3 tools/check_repo.py`；提交前用 `--staged` 检查独立导出的暂存快照。归档探针复跑和代码整理顺序见 [仓库维护](docs/仓库维护.md)。

## 仍开放

- 架构与共享实例：停止态 Debian 只读清单和包事务排队有离线证明。真实同管理根多环境、运行态快照、Arch/RPM 适配、存储引用对账命令、完整共享 APP 生命周期和单实例协议尚未通过。PRE03 已有最小 CLI 的实际覆盖证据，但当前控制器子预算阻断更完整的共享/依赖执行；包库等价性探针未得到结果，WPS 保存重开未启动。固定池的 Debian 与 Arch 不在同一管理根；Arch 候选没有合规内存/CPU 配额。不新建容器、不 `podman update` 来补齐。
- 投影：普通项目直接 bind；只读 FUSE 只浏览停止的环境，活动视图会挡住启动。外部通知 `watcher=false`。严格快照、ACL/xattr、长压、通用 IDE 未过。普通文件页面在每次属性重验证时失效，无重验证的 mmap 读取不保证发现外部修改。
- 投影 rename 两个反例当前均通过，本机真实 FUSE 也覆盖相对链接、嵌套挂载和旧 FD。旧 Python 租约、受限链接目录列举与时间戳碰撞修复及本轮验证范围见 `artifacts/v0.0.1-20261030/report.md`。
- 受管移动只有显式源清理才删源。`handlers.py` 的 provider 仍只有 `environment-app`。
- 版本在 `src/unifier/__init__.py`，`unifier --version` 在本版源码中显示 0.0.3。发布构建入口是 `python3 tools/build_release.py`，当前源码包为 `artifacts/unifier-0.0.3.tar.gz`。升级会保留入口回退链接，步骤见 `docs/v0.0.3.md`。旧包与旧回执保留，不代替本版验收。
- 三宿主跨派系六组合的基础结果已在 `artifacts/server-cross-host-correction-20261030T210239+0800/`，不是完整应用扩大测试。扩大计划仍在 `设计/扩展可用性与Agent跨环境测试计划-2026-10-27.md`，当前修订 3 与机器矩阵在 `artifacts/extension-test-plan-20261031/`；按每宿主两个准入客体安排，旧矩阵保持历史原件。
- 2026-10-31 已获授权实施本机扩测，下载限制解除。[扩测回执](artifacts/extension-local-20261031T111237+0800/report.md) 记录 54 次共享文件回放、12 个 CLI 核心单元、4 个真实 PTY 工作流及两个本地原生包；飞书缺失 ALSA 库导致的 SIGTRAP 已修复并独立确认登录窗口。停止后 z/Z 视图核验、缺失环境误报 ready、TTY 缩放转发和本地包 Unicode 文件名均已修复；该日离线回归 597 项，592 通过、5 跳过。修复现纳入 v0.0.2，部署以 11 月 1 日回执为准，历史未部署状态不代表当前状态。GUI 编辑/保存/账号媒体、Agent 受控桥和长压仍未完成；不能把安装或窗口观察算成完整工作流。两个日常容器及原有暂存条目保留。该日结束时仓库仍有未提交改动；已纳入 v0.0.2 的范围以 11 月 1 日回执为准。
- 2026-10-31 晚：容器安装后自动补 ELF 运行库（apt-file / pacman -F / dnf）和原生安装提示兼容已实现并真实验收：Debian 飞书自动补 14 包、Arch 补 alsa-lib，复查干净，隔离不变；PTY 提示 y/n、`--yes`、`--json` 均通过。经 apt 安装时缩放不传进 dpkg 维护脚本（APT 自带 PTY），未改。见 [回执](artifacts/auto-deps-20261031/report.md)。测试客体 `ml-06a72096-deps-*`、`ml-45dc9c7d-deps-*` 已停止保留。

各轮验收数字、备份路径和失败原件在对应的 `evidence/` 目录，不在这份接手说明里重复。
