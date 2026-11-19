# Unifier

Unifier 在共享宿主内核上运行 Debian 13、Fedora 44 和 Arch 的原生用户空间，各自保留 apt、dnf、pacman。

当前开发候选为 **v0.0.8 Beta 1**（CLI 版本 `0.0.8b1`）。上一正式版本 v0.0.7 包含三目标停止态包清单、原生事务与恢复，以及统一文件访问与授权。正式包来自已验收的 0.0.7.dev0 候选。[v0.0.7 版本说明](docs/v0.0.7.md) · [v0.0.8 真实功能验收计划](设计/v0.0.8-真实功能验收计划-2026-11-17.md)

能力、验收与发布状态统一见 [当前状态](docs/当前状态.md)。正式 v0.0.7 的不可变来源清单在 [release-baseline.json](config/release-baseline.json)；后续源码包记录实际文件摘要和基线来源。真实验收仍按目标、功能与具体版本分别记录。

现行代码在 `src/unifier`。用户入口是 `unifier`（`bin/unifier`，本机已装到 `~/.local/bin/unifier`）。诊断入口是 `python3 tools/unifier.py`。`tools/ml.py` 和 `python -m multilinux` 进入同一实现。布局、证据位置和仍保留的旧标识见 [docs/布局.md](docs/布局.md)。

## 现行说明

- 所有测试先读 [全项目测试禁令与规范](docs/测试禁令与规范.md)；后续默认新增容器预算为 0，限制本轮完整进程树的资源，只能定向停止核验过的测试对象。旧命令示例不自动授权实机执行，代理入口见 [AGENTS.md](AGENTS.md)。
- 架构基线是 [2026-10-23 定档](设计/Unifier-架构实验定档-2026-10-23.md)。定档正文停留在那一天；文件投影、受管复制与移动、项目会话和纯 CLI 已经在现行代码里。
- 用户命令见 [纯 CLI 首次配置与发行版专用目录](设计/CLI首次配置与发行版专用目录-2026-10-29.md)。每个选中的发行版一个日常容器。`unifier init` 做首次选择，`install` / `run` 按发行版安装和启动，`dirs` / `path` / `ls` 查看专用目录。
- 宿主准入见 [发行版派系白名单](设计/发行版派系白名单与容器关系-2026-10-28.md)。客体仍是上面三个锁定目标。查看用 `python3 tools/unifier.py compat list`、`compat check`、`compat matrix`。
- 先后顺序见 [远期规划](设计/远期规划-2026-11-08.md)。先收口这三个目标，再考虑 Alpine、openSUSE 以及更后面的发行版、性能、其他 CPU/GPU 和游戏。那份规划不授权拉镜像或新建容器。
- 宿主与客体同派系时禁止创建和运行容器，Ubuntu → Debian 也拒绝。首次选择仅列出跨派系目标；旧同派系环境保留数据和恢复入口，不自动删除，也不把拒绝转换成宿主执行。
- 同一安装根每个 target 只允许一个容器，底层 `env create --id` 也检查，创建全程加跨进程锁；已承接或停用保留的环境仍占用对应 target。运行态 `ls` 同样检查宿主准入。修复与部署记录位于仓库的 `artifacts/host-authority-fix-20261030/report.md`。
- 目标与镜像锁是 [config/targets.toml](config/targets.toml) 和 [config/images.lock.json](config/images.lock.json)。

已完成的阶段说明和早期调研见仓库的 `archive/notes/README.md`。历史测量、失败记录和冻结源码的恢复方法见仓库的 `archive/README.md`。

## 本机日常环境

上一套日常安装使用 Arch FreeCAD `ml-53c5c8a5-freecad-arch` 和 Debian 腾讯会议 `ml-6b061a2f-meeting`，现行安装状态见[当前状态](docs/当前状态.md)。临时测试环境在结束时按容器身份清理。

Btrfs 上的数据根用文件系统 UUID、子卷身份和目录 inode 识别。见 [腾讯会议目录身份](设计/腾讯会议跨重启目录身份修复-2026-10-28.md)。

## 仍开放

- v0.0.3 增加 `pkg repair-deps --from-log` 和纯本地 `--diagnose-only`，无需手工摘抄明确缺库错误中的 SONAME；ABI/权限/命令/插件问题分类处理，不以补库完成宣称应用可用。该版源码包的独立验收见 组装报告（仓库中的 `artifacts/release-v0.0.3-20261105T152926+0800/report.md`）。以下日期条目保留各阶段的历史范围；这些增量已进入 v0.0.4，验收数字仍属于当时的树。

- 2026-11-05：架构修复 S1–S4 已在工作树实现，整体仍是 **PARTIAL**。停止态 Debian 只读清单、管理根包事务排队、APP subreaper 租约和 bundled 候选分层有有界离线回执，见 首轮报告（仓库中的 `artifacts/architecture-repair-20261105T104613+0800/report.md`）与续轮报告（仓库中的 `artifacts/architecture-repair-20261105T111850+0800/report.md`）。运行态同伴、Arch/RPM 清单、同管理根真实多环境、通用单实例、目标发行版加载器和私有 GUI 没有实机验收。PRE03 不合格，相关 T2 保持 BLOCKED。发布包 unifier-0.0.2.tar.gz（仓库中的 `artifacts/unifier-0.0.2.tar.gz`） 不包含这批增量。2026-11-07 重新组装的 unifier-0.0.3.tar.gz（仓库中的 `artifacts/unifier-0.0.3.tar.gz`） 包含当时的树。11 月 3 日的 726 项全仓检查早于这批改动。2026-11-08 的 unifier-0.0.4.tar.gz（仓库中的 `artifacts/unifier-0.0.4.tar.gz`） 在此之上补上安装后的软件列表登记。12:26 的中止记录保留。最小 Debian 启动/执行/停止和一次停止态清单对齐已经有回执；完整 PRE03、Arch/RPM 清单和同管理根仍未通过。现行顺序见 [远期规划](设计/远期规划-2026-11-08.md)，用例边界仍见 [架构修复计划](docs/架构问题修复计划-20261105.md)和[共享容器专项](docs/共享容器与依赖专项测试计划-20261104.md)。
- 2026-11-03 续测修复控制 FD 在 Ubuntu/runc 下的兼容性、停止实例会话恢复，并加入显式授权的客体请求桥原型。受保护的私有显示下，Writer、Sheet、演示完成两轮保存/停机重开，Beyond Compare 完成选择性合并/保存/重开；字体、菜单和部分关闭行为仍有边界。六个远端请求组合、30 分钟 headless 持久性、本机 726 项及 Python 3.11 回归有独立回执，那些数字描述的是当时的树。这批源码后来进入 v0.0.3 和 v0.0.4，当日没有单独重建发布包。完整结果、桌面事故保护及未完成台账见 续测报告（仓库中的 `artifacts/expansion-continuation-20261103T100648+0800/report.md`）、[受控请求说明](docs/客体受控请求.md)和[扩测保护](docs/扩测保护与恢复.md)。
- 停止环境的只读投影用 FUSE，普通共享项目用直接 bind。外部通知仍是 `watcher=false`。严格快照、ACL/xattr、长压和通用 IDE 未验收。跨宿主已有基础容器/CLI 记录，应用与文件专项仍逐项验收。lookup 锚点有预算，超限明确拒绝。
- v0.0.1 修复旧 Python 租约名称缺失、受限链接阻断目录列举和时间戳碰撞下的重验证。rename 链接/嵌套挂载与旧 FD/mmap 已完成本机真实 FUSE 复验；每次属性重验证都会使普通文件页面失效，性能代价尚未量化。见 本轮记录（仓库中的 `artifacts/v0.0.1-20261030/report.md`）。
- 受管移动在带上显式源清理授权时才删除源。文件处理器目前只有 `environment-app`。
- v0.0.4 的历史便携包位于仓库的 `artifacts/unifier-0.0.4.tar.gz`，附校验和、逐文件清单及独立解包回归日志。[v0.0.4 版本说明](docs/v0.0.4.md)
- 2026-10-31 已获准启动本机扩大测试，下载限制解除，由 dsh Command Code 的 DeepSeek V4.1 Flash 实施、主控独立复核。修订 3 按宿主派系分配：本机先 Debian/WPS、Arch/Zoom，Fedora 客体应用等待非 Fedora 桌面；新增 jq、ripgrep、fd、bat、SQLite、htop 开源 CLI 及 apt/dnf/pacman 包管理专项，apk 单列未实现边界；覆盖同名/同类型应用、Agent、文件回归及 CLI01–CLI14。见 [扩展测试计划](设计/扩展可用性与Agent跨环境测试计划-2026-10-27.md)及本轮授权和执行证据（仓库中的 `artifacts/extension-local-20261031T111237+0800/authorization.json`）。
- 10 月 31 日完成 54 次共享回放、12 个 CLI 核心单元、4 个 PTY 工作流和两个本地原生包补测；飞书登录窗口已确认。旧轮次的 597 项回归和保留客体见 历史报告（仓库中的 `artifacts/extension-local-20261031T111237+0800/report.md`）。这些修复与随后自动补库、原生交互一起纳入 v0.0.2；11 月 1 日新增包事务中断日志、定向进程树清理、未知状态保护和 `pkg recover`。本轮验证与部署见 修复回执（仓库中的 `artifacts/expansion-fix-20261101/report.md`）。完整 GUI 保存/媒体、Agent 与长压仍逐项验收。

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

0.0.7 开发版读取停止态 Fedora 包清单时，还需要运行 Unifier 的宿主 Python 能导入 `rpm` 模块（如 Ubuntu 的 `python3-rpm`）。缺少时明确返回 unsupported，不自动安装宿主软件。支持的数据库格式及限制见 [三目标包管理](docs/三目标包管理收口-20261112.md)。

诊断命令还有 `env`、`pkg`、`app`、`project`、`file`、`view`、`res`。`env create` 的网络默认是 `none`，安装包时加 `--network pasta`。`--desktop` 挂接当前 Wayland、X11 和音频。新导出的桌面项调用 `tools/unifier.py`；已经导出的项里如果还写着 `tools/ml.py`，那条符号链接仍然可用。

已经停止的 rootless Podman 桌面环境可以补配一个宿主有读写权限的 DRM render node。Unifier 在下次启动时挂载该字符设备，保留环境的容器 ID、代际和可写层。在对应安装根运行以下命令，撤销配置时用 `--clear`。

```bash
UNIFIER_HOME="$HOME/.local/share/unifier" unifier env render-node debian13 /dev/dri/renderD128
UNIFIER_HOME="$HOME/.local/share/unifier" unifier env render-node debian13 --clear
```

准备镜像：

```bash
python3 tools/lab.py doctor
python3 tools/lab.py pull
python3 tools/lab.py smoke
```

快捷命令的英文断言要在 `LC_ALL=C LANG=C` 下跑。现场包测试另加 `MULTILINUX_INTEGRATION=1`。
