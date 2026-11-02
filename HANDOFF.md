# Unifier v0.0.2 当前接手入口

现行包是 `src/unifier`，用户入口是 `unifier`，诊断入口是 `python3 tools/unifier.py`。`src/multilinux` 和 `tools/ml.py` 只做旧名字转发。证据在 `archive/evidence`，根目录 `evidence` 是符号链接。已完成的阶段稿和早期调研在 [archive/notes](archive/notes/README.md)。布局见 [docs/布局.md](docs/布局.md)。

分支是 `codex/unifier-v2-control`。产品行为以仓库根 [README.md](README.md) 和 `设计/` 里留下的六份说明为准。

## 接手时先记住

- 2026-11-01 续修：用户指定通过 dsh 的 `Local qwen server` / `swift-1.5-iq3_s` / `max` 负责编码。已独立验收包文件路径保真、实机默认回执保全两项本地模型补丁；另复核保留包 CID、递归补库和进程组清理修复，更新宿主准入与隔离集成测试。本轮 633 项离线检查（628 通过、5 条件跳过）及 Debian/Arch 真实有界探针通过，9 个原容器状态不变；仅工作树，尚未部署。身份、备份、补丁和边界见 [续修回执](artifacts/continued-repair-20261101T154240+0800/report.md)。

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

- 投影：普通项目直接 bind；只读 FUSE 只浏览停止的环境，活动视图会挡住启动。外部通知 `watcher=false`。严格快照、ACL/xattr、长压、通用 IDE 未过。普通文件页面在每次属性重验证时失效，无重验证的 mmap 读取不保证发现外部修改。
- 投影 rename 两个反例当前均通过，本机真实 FUSE 也覆盖相对链接、嵌套挂载和旧 FD。旧 Python 租约、受限链接目录列举与时间戳碰撞修复及本轮验证范围见 `artifacts/v0.0.1-20261030/report.md`。
- 受管移动只有显式源清理才删源。`handlers.py` 的 provider 仍只有 `environment-app`。
- 版本在 `src/unifier/__init__.py`，`unifier --version` 显示 0.0.2。发布构建入口是 `python3 tools/build_release.py`，当前包为 `artifacts/unifier-0.0.2.tar.gz`。升级会保留入口回退链接，步骤见 `docs/v0.0.2.md`。旧包与旧回执保留，不代替本版验收。
- 三宿主跨派系六组合的基础结果已在 `artifacts/server-cross-host-correction-20261030T210239+0800/`，不是完整应用扩大测试。扩大计划仍在 `设计/扩展可用性与Agent跨环境测试计划-2026-10-27.md`，当前修订 3 与机器矩阵在 `artifacts/extension-test-plan-20261031/`；按每宿主两个准入客体安排，旧矩阵保持历史原件。
- 2026-10-31 已获授权实施本机扩测，下载限制解除。[扩测回执](artifacts/extension-local-20261031T111237+0800/report.md) 记录 54 次共享文件回放、12 个 CLI 核心单元、4 个真实 PTY 工作流及两个本地原生包；飞书缺失 ALSA 库导致的 SIGTRAP 已修复并独立确认登录窗口。停止后 z/Z 视图核验、缺失环境误报 ready、TTY 缩放转发和本地包 Unicode 文件名均已修复；该日离线回归 597 项，592 通过、5 跳过。修复现纳入 v0.0.2，部署以 11 月 1 日回执为准，历史未部署状态不代表当前状态。GUI 编辑/保存/账号媒体、Agent 受控桥和长压仍未完成；不能把安装或窗口观察算成完整工作流。两个日常容器及原有暂存条目保留，未提交。
- 2026-10-31 晚：容器安装后自动补 ELF 运行库（apt-file / pacman -F / dnf）和原生安装提示兼容已实现并真实验收：Debian 飞书自动补 14 包、Arch 补 alsa-lib，复查干净，隔离不变；PTY 提示 y/n、`--yes`、`--json` 均通过。经 apt 安装时缩放不传进 dpkg 维护脚本（APT 自带 PTY），未改。见 [回执](artifacts/auto-deps-20261031/report.md)。测试客体 `ml-06a72096-deps-*`、`ml-45dc9c7d-deps-*` 已停止保留。

各轮验收数字、备份路径和失败原件在对应的 `evidence/` 目录，不在这份接手说明里重复。
