# Unifier

**English** · [简体中文](README.zh-CN.md)

Unifier runs Linux applications and command-line tools in a managed Debian, Fedora, or Arch environment. Each environment uses its distribution’s native package manager and runs in rootless Podman on the host Linux kernel.

[Download v0.0.8](https://github.com/Sidler4Hare/Unifier/releases/tag/v0.0.8) · [Release notes (简体中文)](docs/v0.0.8.md)

## Features

- Install packages from a guest’s repositories or from a local package file. Unifier records package transactions and provides diagnostics and recovery for interrupted operations.
- Launch terminal and graphical applications from the CLI. Desktop environments can integrate with the host application menu and file chooser.
- Register host directories for file and project workflows. Unifier can also expose a read-only view of a stopped environment.

## Requirements and compatibility

Use an **x86-64 Linux** host with **Python 3.11 or later**, cgroup v2, and working **rootless Podman**. Desktop applications also require a supported host graphical session. Portal features depend on the host’s XDG Desktop Portal backend. [Desktop integration guide (简体中文)](docs/GTK原生选择器兼容.md)

| Guest | Target ID | Package manager | Local package |
| --- | --- | --- | --- |
| Debian 13 | `debian13` | APT | `.deb` |
| Fedora 44 | `fedora44` | DNF | `.rpm` |
| Arch Linux | `arch` | pacman | `.pkg.tar.zst` |

The host must be on the compatibility whitelist and belong to a different distribution family from the guest. Ubuntu is part of the Debian family. Run `unifier compat matrix` to see the targets available on your host. [Host compatibility policy (简体中文)](设计/发行版派系白名单与容器关系-2026-10-28.md)

## Install

Download `unifier-0.0.8.tar.gz` and `unifier-0.0.8.tar.gz.sha256` from the [v0.0.8 release](https://github.com/Sidler4Hare/Unifier/releases/tag/v0.0.8) into the same directory. Extract the verified archive in a location where you intend to keep Unifier. The installer adds a user-level launcher that points to that directory.

```sh
sha256sum -c unifier-0.0.8.tar.gz.sha256 &&
  tar -xzf unifier-0.0.8.tar.gz &&
  cd unifier-0.0.8 &&
  python3 tools/install_unifier.py &&
  "$HOME/.local/bin/unifier" --version
```

The last command prints `Unifier 0.0.8`. Add `$HOME/.local/bin` to your `PATH` to use the shorter commands below.

## Quick start

On a fresh installation, the following example initializes a Debian 13 guest on a compatible host such as Fedora, installs GNU Hello, and runs it. Initial setup can download the locked guest image and packages.

```sh
export PATH="$HOME/.local/bin:$PATH"
unifier compat check --target debian13
unifier init --target debian13
unifier install --target debian13 hello
unifier run --target debian13 hello
```

To install a local package, use `unifier install ./application.deb`. For graphical applications, pass `--desktop` when initializing the target. On an existing installation, use `unifier configure` to change the selected targets. [First-time setup (简体中文)](设计/CLI首次配置与发行版专用目录-2026-10-29.md)

| Command | Use |
| --- | --- |
| `unifier list` | Show selected targets and environment status. |
| `unifier dirs` | Find the directories assigned to your environments. |
| `unifier start debian13` / `unifier stop debian13` | Start or stop the Debian environment. |
| `unifier help` | Browse commands and options. |

## Upgrade and rollback

Close Unifier-managed applications, extract the new release to a separate directory, and run `python3 tools/install_unifier.py --upgrade` there. Keep the previous directory and the rollback link printed by the installer. Before switching, check any desktop launchers or package-file associations that point to a specific installation directory. Use the same `UNIFIER_HOME` for your existing environments and application registrations. [Upgrade and rollback steps (简体中文)](docs/v0.0.8.md#安装与升级)

## Documentation

- [Package installation and dependency diagnostics (简体中文)](docs/自动依赖与原生安装交互.md)
- [File access and permissions (简体中文)](docs/统一文件访问与权限系统-20261113.md)
- [Project status (简体中文)](docs/当前状态.md)
- [Repository maintenance and test workflow (简体中文)](docs/仓库维护.md)
