# Fedora 桌面宿主本机切片（2026-10-31）

审批见 [approval.json](approval.json)。隔离根是 `~/.local/share/unifier-extension-20261031` 与第二轮的 `~/.local/share/unifier-extension-20261031-r2`。日常根 `~/.local/share/unifier` 未使用。两轮结束后，测试容器已删除；`ml-53c5c8a5-freecad-arch` 与 `ml-6b061a2f-meeting` 的 ID、退出状态和起止时间与开跑前一致。

宿主是 Fedora 44 KDE Wayland。准入客体只有 Debian 13 和 Arch。Fedora 44 客体的 `compat check`、`init` 和 `env create` 都被策略拒绝，且没有新容器。入口版本为 Unifier 0.0.1。锁定镜像已在本机，init 未再拉取。

## 已执行

| 项 | 结果 |
| --- | --- |
| CLI 帮助、未知命令、`--version`、显式英文 | 通过 |
| `init --desktop --network pasta` 创建 Debian 13 与 Arch | 通过。记录为 keep-id，专用 files 为同一路径 bind，选项 `z` |
| 每个 files 目录两轮写入、停机、再开 | 内容通过，含中文文件。客体内 `env run` 写的文件属主是 524288，不是宿主用户 omg |
| 运行态 `ls /` | 通过 |
| 停止态 `ls /` | 失败。见下方 |
| 桌面套接字 | Wayland、X11、Pulse、PipeWire 四个套接字在两个客体里都是 socket。没有启动图形程序 |
| Debian 仓库 jq 1.7 | 通过。`jq -s` 求和为 5，非法 JSON 退出码 5 |
| Arch 全量升级后安装 jq | 通过。待升级下载约 26.7 MiB（17 个包），低于本轮 80 MiB 上限；求和 oracle 为 5 |

`env run` 的进程是 `uid=0`、`HOME=/root`，且不带 `WAYLAND_DISPLAY`。套接字挂载和会话变量是两件事。

## 停止态目录

两个客体的停止态 `ls` 都失败，原文是 `BACKEND_UNAVAILABLE: frozen share options changed`。记录里的 files 选项是 `z`，Podman 对该挂载报告的选项只有 `rbind`。视图要求记录中的选项是 Podman 选项的子集，所以 SELinux 的 `z` 使停止态只读视图在本机无法打开。运行态列举不受这个检查影响。

## 收尾缺口

诊断命令 `env remove` 删掉了容器和环境记录，快捷 `list` 仍把 Debian 13 和 Arch 标成 ready、files 已挂载。容器标签是 `—`，`failed` 为空。见 [list-after-env-remove.json](list-after-env-remove.json)。

## 今天没有跑

WPS、Zoom、飞书、Beyond Compare 没有可安装的本机 DEB 或 Arch 包，下载目录里只有 WPS RPM。没有 Fedora 客体、三台服务器、Agent 跨环境控制，也没有两个客体共用一个项目目录的 6 边互写。那些仍是 NOT_RUN。
