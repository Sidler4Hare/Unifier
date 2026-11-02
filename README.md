# Unifier v0.0.2

Unifier 在共享宿主内核上运行 Debian 13、Fedora 44 和 Arch 的原生用户空间，各自保留 apt、dnf、pacman。

当前为 v0.0.2 测试版，包含包事务中断恢复、自动补运行库与原生安装交互。安装、升级、回退和能力限制见 [版本说明](docs/v0.0.2.md)。`unifier --version` 可核对版本；精确源码以发布包内清单为准。

现行代码在 `src/unifier`。用户入口是 `unifier`（`bin/unifier`，本机已装到 `~/.local/bin/unifier`）。诊断入口是 `python3 tools/unifier.py`。`tools/ml.py` 和 `python -m multilinux` 进入同一实现。布局、证据位置和仍保留的旧标识见 [docs/布局.md](docs/布局.md)。

## 现行说明

- 架构基线是 [2026-10-23 定档](设计/Unifier-架构实验定档-2026-10-23.md)。定档正文停留在那一天；文件投影、受管复制与移动、项目会话和纯 CLI 已经在现行代码里。
- 用户命令见 [纯 CLI 首次配置与发行版专用目录](设计/CLI首次配置与发行版专用目录-2026-10-29.md)。每个选中的发行版一个日常容器。`unifier init` 做首次选择，`install` / `run` 按发行版安装和启动，`dirs` / `path` / `ls` 查看专用目录。
- 宿主准入见 [发行版派系白名单](设计/发行版派系白名单与容器关系-2026-10-28.md)。客体仍是上面三个锁定目标。查看用 `python3 tools/unifier.py compat list`、`compat check`、`compat matrix`。
- 宿主与客体同派系时禁止创建和运行容器，Ubuntu → Debian 也拒绝。首次选择仅列出跨派系目标；旧同派系环境保留数据和恢复入口，不自动删除，也不把拒绝转换成宿主执行。
- 同一安装根每个 target 只允许一个容器，底层 `env create --id` 也检查，创建全程加跨进程锁；已承接或停用保留的环境仍占用对应 target。运行态 `ls` 同样检查宿主准入。修复与部署记录见 [母发行版权威修复](artifacts/host-authority-fix-20261030/report.md)。
- 目标与镜像锁是 [config/targets.toml](config/targets.toml) 和 [config/images.lock.json](config/images.lock.json)。

已完成的阶段说明和早期调研在 [archive/notes](archive/notes/README.md)。测量、失败记录和冻结源码在 `evidence/`（指向 `archive/evidence`）。

## 本机日常环境

日常容器是 Arch FreeCAD `ml-53c5c8a5-freecad-arch` 和 Debian 腾讯会议 `ml-6b061a2f-meeting`。临时测试环境在结束时按容器身份清理。

Btrfs 上的数据根用文件系统 UUID、子卷身份和目录 inode 识别。见 [腾讯会议目录身份](设计/腾讯会议跨重启目录身份修复-2026-10-28.md)。

## 仍开放

- 停止环境的只读投影用 FUSE，普通共享项目用直接 bind。外部通知仍是 `watcher=false`。严格快照、ACL/xattr、长压和通用 IDE 未验收。跨宿主已有基础容器/CLI 记录，应用与文件专项仍逐项验收。lookup 锚点有预算，超限明确拒绝。
- v0.0.1 修复旧 Python 租约名称缺失、受限链接阻断目录列举和时间戳碰撞下的重验证。rename 链接/嵌套挂载与旧 FD/mmap 已完成本机真实 FUSE 复验；每次属性重验证都会使普通文件页面失效，性能代价尚未量化。见 [本轮记录](artifacts/v0.0.1-20261030/report.md)。
- 受管移动在带上显式源清理授权时才删除源。文件处理器目前只有 `environment-app`。
- 当前便携源码包是 [unifier-0.0.2.tar.gz](artifacts/unifier-0.0.2.tar.gz)，附校验和、逐文件清单及独立解包回归日志。旧包保持历史原件。步骤见 [版本说明](docs/v0.0.2.md)；具体宿主验证范围以本轮记录为准。
- 2026-10-31 已获准启动本机扩大测试，下载限制解除，由 dsh Command Code 的 DeepSeek V4.1 Flash 实施、主控独立复核。修订 3 按宿主派系分配：本机先 Debian/WPS、Arch/Zoom，Fedora 客体应用等待非 Fedora 桌面；新增 jq、ripgrep、fd、bat、SQLite、htop 开源 CLI 及 apt/dnf/pacman 包管理专项，apk 单列未实现边界；覆盖同名/同类型应用、Agent、文件回归及 CLI01–CLI14。见 [扩展测试计划](设计/扩展可用性与Agent跨环境测试计划-2026-10-27.md)及[本轮授权和执行证据](artifacts/extension-local-20261031T111237+0800/authorization.json)。
- 10 月 31 日完成 54 次共享回放、12 个 CLI 核心单元、4 个 PTY 工作流和两个本地原生包补测；飞书登录窗口已确认。旧轮次的 597 项回归和保留客体见 [历史报告](artifacts/extension-local-20261031T111237+0800/report.md)。这些修复与随后自动补库、原生交互一起纳入 v0.0.2；11 月 1 日新增包事务中断日志、定向进程树清理、未知状态保护和 `pkg recover`。本轮验证与部署见 [修复回执](artifacts/expansion-fix-20261101/report.md)。完整 GUI 保存/媒体、Agent 与长压仍逐项验收。

## 常用命令

安装后自动补齐未声明 ELF 运行库，并支持原生包管理器的终端交互。用法、失败语义和适用边界见 [自动依赖与原生安装交互](docs/自动依赖与原生安装交互.md)；旧 v0.0.1 包不包含该功能。

```bash
python3 tools/install_unifier.py
unifier init
unifier --version
python3 tools/unifier.py --help
python3 tools/check_repo.py
python3 tools/check_repo.py --staged
```

需要 Python 3.11 或更新版本。`check_repo.py` 统一离线检查；`--staged` 独立导出暂存内容，检查提交是否漏了新模块。归档探针使用 `tools/run_archived_probe.py` 复跑，详见 [仓库维护](docs/仓库维护.md)。

诊断命令还有 `env`、`pkg`、`app`、`project`、`file`、`view`、`res`。`env create` 的网络默认是 `none`，安装包时加 `--network pasta`。`--desktop` 挂接当前 Wayland、X11 和音频。新导出的桌面项调用 `tools/unifier.py`；已经导出的项里如果还写着 `tools/ml.py`，那条符号链接仍然可用。

准备镜像：

```bash
python3 tools/lab.py doctor
python3 tools/lab.py pull
python3 tools/lab.py smoke
```

快捷命令的英文断言要在 `LC_ALL=C LANG=C` 下跑。现场包测试另加 `MULTILINUX_INTEGRATION=1`。
