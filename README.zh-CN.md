# Unifier

[English](README.md) · **简体中文**

Unifier 可在受管的 Debian、Fedora 或 Arch 环境中运行 Linux 应用和命令行工具。各环境保留本发行版的原生包管理器，在 rootless Podman 中共享宿主 Linux 内核。

[下载 v0.0.8](https://github.com/Sidler4Hare/Unifier/releases/tag/v0.0.8) · [版本说明](docs/v0.0.8.md)

## 主要功能

- 从客体仓库或本地安装包安装软件。Unifier 记录包事务，提供安装诊断和中断恢复。
- 从命令行启动终端应用和图形应用。桌面环境可接入宿主应用菜单与文件选择器。
- 登记宿主目录，用于文件和项目操作。已停止的环境可以按需打开只读视图。

## 系统要求与兼容性

需要 **x86-64 Linux** 宿主、**Python 3.11 或更高版本**、cgroup v2，以及可用的 **rootless Podman**。运行图形应用还需要受支持的宿主图形会话。Portal 功能取决于宿主的 XDG Desktop Portal 后端。[桌面集成指南](docs/GTK原生选择器兼容.md)

| 客体 | 目标 ID | 包管理器 | 本地包格式 |
| --- | --- | --- | --- |
| Debian 13 | `debian13` | APT | `.deb` |
| Fedora 44 | `fedora44` | DNF | `.rpm` |
| Arch Linux | `arch` | pacman | `.pkg.tar.zst` |

宿主须在兼容白名单内，并与客体分属不同发行版派系。Ubuntu 属于 Debian 派系。运行 `unifier compat matrix` 可查看当前宿主可用的目标。[宿主兼容规则](设计/发行版派系白名单与容器关系-2026-10-28.md)

## 安装

从 [v0.0.8 发布页](https://github.com/Sidler4Hare/Unifier/releases/tag/v0.0.8) 下载 `unifier-0.0.8.tar.gz` 及同名 `.sha256` 文件，放在同一个目录。校验后解压到准备长期保留的位置。安装脚本会添加指向该目录的用户级命令入口。

```sh
sha256sum -c unifier-0.0.8.tar.gz.sha256 &&
  tar -xzf unifier-0.0.8.tar.gz &&
  cd unifier-0.0.8 &&
  python3 tools/install_unifier.py &&
  "$HOME/.local/bin/unifier" --version
```

最后一条命令应输出 `Unifier 0.0.8`。将 `$HOME/.local/bin` 加入 `PATH` 后，即可使用下文的 `unifier` 命令。

## 快速开始

下面以首次安装的 Fedora 等兼容宿主为例，初始化 Debian 13 客体，安装并运行 GNU Hello。首次准备可能需要下载锁定的客体镜像和软件包。

```sh
export PATH="$HOME/.local/bin:$PATH"
unifier compat check --target debian13
unifier init --target debian13
unifier install --target debian13 hello
unifier run --target debian13 hello
```

安装本地包可使用 `unifier install ./application.deb`。如需图形应用，初始化目标时加 `--desktop`。已有安装可用 `unifier configure` 调整已选目标。[首次配置](设计/CLI首次配置与发行版专用目录-2026-10-29.md)

| 命令 | 用途 |
| --- | --- |
| `unifier list` | 查看已选目标及环境状态。 |
| `unifier dirs` | 查找各环境使用的目录。 |
| `unifier start debian13` / `unifier stop debian13` | 启动或停止 Debian 环境。 |
| `unifier help` | 查看命令和参数。 |

## 升级与回退

先正常退出由 Unifier 管理的应用，把新版本解压到独立目录，再在该目录执行 `python3 tools/install_unifier.py --upgrade`。保留旧版本目录和脚本输出的回退链接。切换前核对写死安装路径的桌面入口和软件包文件关联。原环境和应用登记继续使用同一个 `UNIFIER_HOME`。[升级与回退步骤](docs/v0.0.8.md#安装与升级)

## 相关文档

- [软件包安装与依赖诊断](docs/自动依赖与原生安装交互.md)
- [文件访问与权限](docs/统一文件访问与权限系统-20261113.md)
- [项目当前状态](docs/当前状态.md)
- [仓库维护与测试流程](docs/仓库维护.md)
