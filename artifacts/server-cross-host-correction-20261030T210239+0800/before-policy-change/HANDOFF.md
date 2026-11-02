# Unifier 当前接手入口

现行包是 `src/unifier`，用户入口是 `unifier`，诊断入口是 `python3 tools/unifier.py`。`src/multilinux` 和 `tools/ml.py` 只做旧名字转发。证据在 `archive/evidence`，根目录 `evidence` 是符号链接。已完成的阶段稿和早期调研在 [archive/notes](archive/notes/README.md)。布局见 [docs/布局.md](docs/布局.md)。

分支是 `codex/unifier-v2-control`。产品行为以仓库根 [README.md](README.md) 和 `设计/` 里留下的六份说明为准。

## 接手时先记住

- 日常容器只有两个：FreeCAD `ml-53c5c8a5-freecad-arch`，腾讯会议 `ml-6b061a2f-meeting`。测试容器用完即清。
- 容器标签、容器名、MIME、桌面 ID、容器内路径、`.ml-` 前缀、投影 inode 盐和 `MULTILINUX_*` 标记保持原拼写。探针线协议 `multilinux.*.v1` 也保持原样。
- `archive/evidence` 的字节保持原样。报告里的旧路径是归档时的路径。
- 物理工作区是 `/home/omg/文档/多兼容linux机`。`/home/omg/文档/Unifier` 指向它。路径参与环境身份，搬动物理目录会改变身份。
- 快捷命令测试按英文片段断言。本机 locale 是中文时，用 `LC_ALL=C LANG=C` 跑 `tests/test_cli_shortcuts.py`。
- 统一离线入口是 `python3 tools/check_repo.py`；提交前用 `--staged` 检查独立导出的暂存快照。归档探针复跑和代码整理顺序见 [仓库维护](docs/仓库维护.md)。

## 仍开放

- 投影：普通项目直接 bind；只读 FUSE 只浏览停止的环境，活动视图会挡住启动。外部通知 `watcher=false`。严格快照、ACL/xattr、长压、通用 IDE、跨宿主未过。
- 投影外部 rename 的两个反例未修：相对链接仍用旧父路径，目录可能继承错误的嵌套挂载。用维护文档里的复跑命令可见两个失败；不要把返回 1 当作通过。
- 受管移动只有显式源清理才删源。`handlers.py` 的 provider 仍只有 `environment-app`。
- Ubuntu Server 便携包停在 2026-10-28，不含投影 FD 修复和新 CLI。步骤在 `设计/Ubuntu-Server白名单与容器验收-2026-10-28.md`。
- 扩大测试计划修订到 2026-10-29，整套安排在 2026 年 11 月。三台真实宿主按用户报告为进行中，结果未导入。计划在 `设计/扩展可用性与Agent跨环境测试计划-2026-10-27.md`，矩阵在 `evidence/extension-test-plan-2026-10-29/`。

各轮验收数字、备份路径和失败原件在对应的 `evidence/` 目录，不在这份接手说明里重复。
