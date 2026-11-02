# 白名单模式日常部署与菜单验收

日期：2026-10-28，Asia/Shanghai。

结论：**FreeCAD、腾讯会议均已启用白名单，分别连续两轮通过原菜单启动、完整窗口、正常关窗和自动停机；现有容器、应用安装和文档保留。** Ubuntu Server 实机验证仍待下一台机器执行。

## 部署与恢复点

两个部署均更新白名单所需的 `compatibility.py`、`service.py`、`cli.py`、`project_sessions.py` 和 `tools/lab.py`，在各自原 `targets.toml` 中增加 `compatibility.mode=whitelist`。没有复制仓库的通用镜像锁去覆盖日常派生镜像，也没有重建容器或登记新身份。腾讯会议先前的 Btrfs 目录身份修复继续保留。

| 应用 | 原 CID 保留 | 目标 | 原菜单 |
| --- | --- | --- | --- |
| FreeCAD | `37daa7f7ff06042b482e941b34fd1a8f2a6a811a8e3bba65bac2a9de75346cd5` | Arch | `unifier-freecad.desktop` |
| 腾讯会议 | `b9dd15c5fccf9f02a1033d6c824066563713fb3f8cfe5bea37b73378417cefdb` | Debian 13 | `wemeetapp.desktop` |

备份：`/home/omg/.local/state/unifier-deployments/whitelist-20261028-202845/`，权限受限。两个完整部署目录、原菜单、文档目录逐文件复制并校验；容器私有 HOME 与原生包数据库从停止容器只读归档，逐成员解析并保存 SHA-256。备份、原身份和部署清单见 [context.json](context.json)；详细私有文件目录留在备份内，不写入报告。

必要模块以原生命周期/租约锁保护、原子替换。配置语义比较确认只增加白名单模式。部署阶段在真实已安装 CLI 上执行关系矩阵，原状态、菜单和文档未改写。最初部署回执为 [deployment.json](deployment.json)，随后会话挂载修复见 [session-mount-repair.json](session-mount-repair.json)；最终部署 manifest 已记录两部分源码哈希。

## 启动中发现并修复的问题

FreeCAD 第一次菜单启动通过了白名单，但 crun 在启动容器前拒绝：原绑定源 `/run/user/1000/xauth_NCgtEd` 已不存在，当前会话的 Xauthority 名称已变化。首次失败日志保留在 [freecad-menu/launch-1.log](freecad-menu/launch-1.log)。

新增 `desktop.restore_xauthority_mount()`，在 `EnvironmentService.start()` 的生命周期锁内、真正启动前处理已登记的旧 `xauth_*` 会话路径。只在宿主用户私有 runtime 目录中，为缺失路径建立到当前已校验会话文件的相对链接；不读取/打印 cookie、不覆盖已有文件或外来链接，不更换容器挂载契约。两个部署均装入该修复。当前 FreeCAD 已恢复相同 CID 的原容器启动。

新增 6 项反例和接入测试，覆盖缺失别名、已有文件不覆盖、外来链接拒绝、当前文件/目录权限、无桌面无副作用和失败先于 runtime.start。最终完整回归 **449 项，445 通过、4 条件跳过**，见 [unit-tests-final.log](unit-tests-final.log)。没有进行新的真实宿主重启；本次是当前会话中真实缺失路径的修复和实际应用复验。

旧临时 KWin 探针没有返回结果，首次严格截图检查也遇到抓图后焦点变化；这些观察失败没有杀应用或强停容器。相应尝试及正常关闭回执保留。最终使用本机已有 `kos-platform.sock` 的 KWin 窗口桥接，按宿主 PID、应用 ID、窗口 UUID 定位、激活和正常关闭；截图逐张目视确认完整目标窗口。该桥接源于本机 KOS 的公开本地代码路径，没有安装新 KWin 插件或改变桌面设置。

## 最终菜单验收

真实入口均为 `gtk-launch` 调用原 `.desktop`，不是手工运行容器中的可执行文件。每轮核对真实窗口、白名单模式、原 CID 和应用租约，目视确认后发送窗口管理器正常关闭请求，等待最后租约释放和容器自动停止；没有用 kill 或 `env stop` 代替验收。

| 应用 | 轮次 | 菜单/完整窗口 | 正常关窗后 | 证据 |
| --- | --- | --- | --- | --- |
| FreeCAD | 1、2 | 均通过，完整 FreeCAD 1.1.4 主窗口 | 均 stopped，租约 0 | [最终回执](freecad-menu-attempt4/result.json)、[首次截图](freecad-menu-attempt4/launch-1.png)、[重开截图](freecad-menu-attempt4/launch-2.png) |
| 腾讯会议 | 1、2 | 均通过，完整腾讯会议登录窗口 | 均 stopped，租约 0，portal bridge 清理 | [最终回执](wemeet-menu/result.json)、[首次截图](wemeet-menu/launch-1.png)、[重开截图](wemeet-menu/launch-2.png) |

本轮没有接受登录协议、登录、入会、上传文件或验证摄像头/音频/共享屏幕。

## 数据与最终状态

[最终保全审计](audit.json) 为 `PASSED_DEPLOYMENT_AND_DATA_PRESERVATION`：

- 两容器 CID、镜像、挂载和容器配置保持一致；UUID/代际、应用实例、目标/包驱动等环境契约保留。
- 原菜单、文档目录、镜像锁和非生命周期状态文件逐项一致；FreeCAD 10 个、腾讯会议 16 个登记/清单文件保持原哈希。环境记录只允许正常生命周期的更新时间、运行状态及观察内核字段变化。
- Arch 包数据库 1,415 个条目、Debian 包数据库 1,998 个条目前后相同。没有安装、卸载或升级应用包。
- FreeCAD 私有 HOME 完全一致。腾讯会议正常运行更新了 13 个运行数据库/偏好/日志文件，新增 18 个条目；6 个历史日志被应用压缩为 ZIP，解压内容哈希与原日志一致；2 个旧动态资源缓存由应用退役，原件仍保留在已校验的运行前 HOME 归档。没有发现其他原有私有文件丢失，文档未变。
- 两个日常容器均自动停止、租约为空，腾讯会议门户代理清理；最终仅原有两个容器，无常驻测试实例。

初次私有 HOME 审计将日志压缩/缓存退役当作删除而失败；随后按 ZIP 内容逐一核对，并将旧动态资源单独列为缓存与备份保全，未通过恢复旧缓存或覆盖新的应用偏好来掩盖正常运行变化。原始和复核后的私有目录差异均留在备份中。

回退说明在备份的 `RESTORE.md`：先正常关闭应用，再按同一检查点恢复必要代码、`targets.toml` 和部署 manifest；不回写过期生命周期字段，不盲目替换应用私有 HOME。备份不是完整磁盘/进程恢复演练。

## Ubuntu Server 交付

新增 [`tools/probe_headless.py`](../../tools/probe_headless.py) 和 [Ubuntu Server 步骤](../../设计/Ubuntu-Server白名单与容器验收-2026-10-28.md)。普通用户、Linux x86_64、Python 3.11+、rootless Podman/cgroup v2 路径；先 `compat`、`doctor`、拉取锁定镜像，再运行无桌面工具。无需桌面环境、Xauthority、portal 或 FUSE。

便携包：[`unifier-ubuntu-server-20261028.tar.gz`](../../artifacts/unifier-ubuntu-server-20261028.tar.gz)，479,438 字节，124 个源码/配置/测试/说明文件，另含逐文件 manifest。

SHA-256：`39699b4f8002ce9128fca9c745a9a9259390c57e4fc543d325f610d28a77c51a`，配套 [校验文件](../../artifacts/unifier-ubuntu-server-20261028.tar.gz.sha256)。不包含当前机器 `.lab`、私有 HOME、登录信息、日常应用数据或容器镜像。

首次打包遗漏了 `tools/unifier.py` 的源码内别名，已保留失败包并修正为只解引用指向仓库内部的文件链接。最终包重新解压并核对全部哈希；在移除 DISPLAY、WAYLAND_DISPLAY、XAUTHORITY、DBUS_SESSION_BUS_ADDRESS 等桌面变量后，入口、20 项白名单契约及 doctor 通过。随后从**解压后的最终包**实际创建/启动/执行/查询/停机重开/删除三套容器全部通过，既有容器启停时间未变，见 [无桌面回执](server-bundle-headless.json)、[前置检查](server-bundle-preflight.json) 和 [包清单](server-bundle.json)。

该复验的真实宿主仍为 Fedora；Ubuntu 的真实宿主准入、创建和启动必须到那台服务器运行，不能提前记为通过。未提交/推送。
