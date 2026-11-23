# Unifier

Unifier 在共享宿主内核上运行 Debian 13、Fedora 44 和 Arch 的原生用户空间，各自保留 apt、dnf、pacman。

当前正式版本为 **Unifier v0.0.8**，CLI 显示 `0.0.8`。以 `v0.0.8` 标签和对应的 GitHub Release 为准。下载安装使用 Releases 中随附清单与 SHA-256 的 `unifier-0.0.8.tar.gz`。[下载正式版](https://github.com/Sidler4Hare/Unifier/releases/tag/v0.0.8) · [版本说明](docs/v0.0.8.md)

能力、实测范围和安装状态见 [当前状态](docs/当前状态.md)。本次发布包的构建参照是 [v0.0.7 冻结清单](config/release-baseline.json)。

现行代码在 `src/unifier`。用户入口是 `unifier`（`bin/unifier`）。诊断入口是 `python3 tools/unifier.py`。`tools/ml.py` 和 `python -m multilinux` 进入同一实现。布局、证据位置和仍保留的旧标识见 [docs/布局.md](docs/布局.md)。

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

## 验收与历史

- 当前能力、候选、本机部署与适用范围见 [当前状态](docs/当前状态.md)。具体验收按 [v0.0.8 真实功能验收计划](设计/v0.0.8-真实功能验收计划-2026-11-17.md)执行。
- 包清单与事务恢复见 [三目标包管理](docs/三目标包管理收口-20261112.md)。受管文件、授权及只读投影见 [统一文件访问与权限](docs/统一文件访问与权限系统-20261113.md)。
- 历史阶段材料见 [归档索引](https://github.com/Sidler4Hare/Unifier/blob/v0.0.8/archive/notes/README.md)和[恢复说明](https://github.com/Sidler4Hare/Unifier/blob/v0.0.8/archive/README.md)。旧发布包及验收记录见 [v0.0.4 版本说明](docs/v0.0.4.md)和[验收材料](https://github.com/Sidler4Hare/Unifier/blob/v0.0.8/artifacts/README.md)。

## 常用命令

[命令体系设计](设计/Unifier命令体系-2026-11-23.md)记录 v0.0.9 拟采用的管理入口、兼容迁移及验收。

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

读取停止态 Fedora 包清单时，还需要运行 Unifier 的宿主 Python 能导入 `rpm` 模块（如 Ubuntu 的 `python3-rpm`）。缺少时明确返回 unsupported，不自动安装宿主软件。支持的数据库格式及限制见 [三目标包管理](docs/三目标包管理收口-20261112.md)。

诊断命令还有 `env`、`pkg`、`app`、`project`、`file`、`view`、`res`。`env create` 的网络默认是 `none`，安装包时加 `--network pasta`。`--desktop` 挂接当前 Wayland、X11 和音频。新导出的桌面项调用 `tools/unifier.py`；已经导出的项里如果还写着 `tools/ml.py`，那条符号链接仍然可用。

`unifier env desktop-integration debian13` 查看宿主的 Portal 后端配置、当前接口和客体依赖。带 `--install CLIENT` 时，通过该客体的原生包管理器补客户端。GTK、Qt 和媒体客户端共用宿主当前的 KDE、GNOME 或其他 Portal 服务。[桌面客户端用法](docs/GTK原生选择器兼容.md)

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
