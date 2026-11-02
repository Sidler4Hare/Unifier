# 容器自动补依赖 + 原生安装提示兼容：验收回执（2026-10-31，接替 Codex）

需求：飞书暴露的问题 → 容器装完软件后自己补齐缺失的运行库，不靠手动下载；命令行安装时原生包管理器的提示要能照常交互。

## 代码（未提交，留在工作树）
- `src/unifier/dependencies.py`（新）：安装后扫描包内 ELF（含厂商插件目录，不执行厂商程序）的 NEEDED，缺失库到客体自己的仓库找提供者（Debian `apt-file`，Arch `pacman -F`，Fedora `dnf`），找到唯一候选才装，记入包事务；多候选/无候选明确报错。Codex 最后一处补丁（apt Contents 路径是相对路径，正则去掉开头 `/`）已确认生效。
- `src/unifier/native_io.py`（新）：TTY 下保留原生提示（PTY 转发输入、缩放），`--yes` 走非交互，`--json` 输出干净。
- `cli.py`、`packages.py`、`service.py`、`runtime.py` 接入；测试 `tests/test_dependencies.py`、`tests/test_native_io.py` 等。
- 本轮接手没有再改 src/，只做验证。

## 离线测试
- 聚焦 4 组（dependencies/native_io/packages/cli_shortcuts）：81 项全过（`focused-takeover.log`）。
- 全量：609 项，604 过 5 跳过；`tools/check_repo.py` PASS（`check-repo-final.log`）。
- 注意：中文 locale 下有 15 项因按英文字句断言而失败，属已知测试约定（HANDOFF 已写），用 `LC_ALL=C LANG=C` 跑。

## 真实容器（全部串行，新建独立实验目录 `.lab/auto-deps-20261031-r2`）
- Debian 13 全新客体装本地飞书 .deb：`txn15c7de5ac065` verified。自动找到并安装 14 个包（含 `libasound2t64`、`libgtk-3-0t64`、`libcups2t64` 等），unresolved 为空。宿主 rpm 清单前后一致，未新增宿主条目。证据 `feishu-install-fresh.json/.log`。
- 复查：再跑 repair-deps → `txn5e21a8f94deb` `no-missing-elf-dependencies`，干净（`feishu-recheck.json`）。
- Arch 全新客体：`libasound.so.2` → `alsa-lib` 自动安装，`txn1837ac6cca2f` verified（`arch-repair-r2.json/.log`）。
- 隔离：Arch 事务记录的 Debian 客体清单哈希前后不变（unchanged=true）；宿主所有原有容器（FreeCAD、腾讯会议等）前后无变化（`containers-before-rerun.json` 对比）。
- 上一轮 Codex 在旧实验目录里的串行修复 `txn88965f94ebaa` 同样成功（`feishu-repair-serial.json`）。
- `runtime_loading` 仍标 `unverified`：只证明库已装齐，本轮没再启动飞书窗口（需 GUI，之前回执已看到登录窗口）。

## 原生提示（真实 PTY，`prompt/`）
- apt `[Y/n]` 答 n：退出码 1，`failure_phase=native-command`，包清单未变（`apt-no.json`）。
- 答 Y：退出码 0，cowsay 已装（`apt-yes.json`）。
- 自定义 .deb 的 postinst 读 /dev/tty：提示透传，输入 `hello` 被客体收到（`fixture.json`、`guest-state.txt`）。
- `--yes` 无 TTY：退出 0，sl 已装；`--yes --json`：stdout 是可解析 JSON（`txne0abfcfa636f`），进度只在 stderr。
- 退出后终端恢复正常，无残留进程。
- 终端缩放：Unifier 自身的 PTY 层和 `podman exec -it` 都能把 24×80→38×120 传进去（`resize_probe.py` 三层对照）。**但经 apt-get 安装时，dpkg 维护脚本看到的仍是旧尺寸**；同一个包直接 `dpkg -i` 能看到新尺寸。原因是 APT 自己给 dpkg 开了一层 PTY，不转发缩放，属 APT 上游行为。

## 仍开放
1. apt 内缩放：可选改法是交互模式给 apt 加 `-o Dpkg::Use-Pty=0`（代价是没有 apt 进度条）。未改，等决定。
2. 飞书补库后的真实窗口启动复核（需 GUI）；Fedora 客体本轮没有做真实运行（宿主是 Fedora，同派系策略禁止）。
3. 测试客体保留（已停止）：`ml-06a72096-deps-debian`、`ml-06a72096-deps-arch`（新），以及 Codex 的 `ml-45dc9c7d-deps-debian/arch`；确认后按精确身份清理。
4. 未提交、未部署；原有暂存条目没动。
