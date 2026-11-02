# Unifier

Unifier 在共享宿主内核上运行 Debian 13、Fedora 44 和 Arch 的原生用户空间，各自保留 apt、dnf、pacman。

现行代码在 `src/unifier`。用户入口是 `unifier`（`bin/unifier`，本机已装到 `~/.local/bin/unifier`）。诊断入口是 `python3 tools/unifier.py`。`tools/ml.py` 和 `python -m multilinux` 进入同一实现。布局、证据位置和仍保留的旧标识见 [docs/布局.md](docs/布局.md)。

## 现行说明

- 架构基线是 [2026-10-23 定档](设计/Unifier-架构实验定档-2026-10-23.md)。定档正文停留在那一天；文件投影、受管复制与移动、项目会话和纯 CLI 已经在现行代码里。
- 用户命令见 [纯 CLI 首次配置与发行版专用目录](设计/CLI首次配置与发行版专用目录-2026-10-29.md)。每个选中的发行版一个日常容器。`unifier init` 做首次选择，`install` / `run` 按发行版安装和启动，`dirs` / `path` / `ls` 查看专用目录。
- 宿主准入见 [发行版派系白名单](设计/发行版派系白名单与容器关系-2026-10-28.md)。客体仍是上面三个锁定目标。查看用 `python3 tools/unifier.py compat list`、`compat check`、`compat matrix`。
- 目标与镜像锁是 [config/targets.toml](config/targets.toml) 和 [config/images.lock.json](config/images.lock.json)。

已完成的阶段说明和早期调研在 [archive/notes](archive/notes/README.md)。测量、失败记录和冻结源码在 `evidence/`（指向 `archive/evidence`）。

## 本机日常环境

日常容器是 Arch FreeCAD `ml-53c5c8a5-freecad-arch` 和 Debian 腾讯会议 `ml-6b061a2f-meeting`。临时测试环境在结束时按容器身份清理。

Btrfs 上的数据根用文件系统 UUID、子卷身份和目录 inode 识别。见 [腾讯会议目录身份](设计/腾讯会议跨重启目录身份修复-2026-10-28.md)。

## 仍开放

- 停止环境的只读投影用 FUSE。普通共享项目用直接 bind。外部修改通知仍是 `watcher=false`。严格快照、ACL/xattr、长压、通用 IDE 和跨宿主还没有验收。目录浏览按名称记账；lookup 锚点仍有预算，超限会明确拒绝。记录在 `evidence/projection-fd-repair-2026-10-29/` 和 `evidence/projection-boundaries-2026-10-27/`。
- 投影外部 rename 后的相对链接和嵌套挂载路径仍有已复现缺陷。复跑入口与后续代码整理边界见 [仓库维护](docs/仓库维护.md)。
- 受管移动在带上显式源清理授权时才删除源。文件处理器目前只有 `environment-app`。
- Ubuntu Server 便携包 [artifacts/unifier-ubuntu-server-20261028.tar.gz](artifacts/unifier-ubuntu-server-20261028.tar.gz) 是 10 月 28 日的冻结，里面没有其后的投影 FD 修复和新 CLI。步骤见 [无桌面验收](设计/Ubuntu-Server白名单与容器验收-2026-10-28.md)。三台真实宿主按用户报告已经开测，结果还没有记为通过。
- 扩大测试安排在 2026 年 11 月，日期未定。范围包括 WPS、Zoom、飞书、同名与同类型应用，以及 CLI01–CLI12。见 [扩展测试计划](设计/扩展可用性与Agent跨环境测试计划-2026-10-27.md)。

## 常用命令

```bash
python3 tools/install_unifier.py
unifier init
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
