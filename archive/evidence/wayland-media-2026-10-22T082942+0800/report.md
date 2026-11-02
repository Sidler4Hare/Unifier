# 原生 Wayland、剪贴板、音频与腾讯会议实测

2026-10-22T08:29:42+08:00 开始，2026-10-22T09:20:00+08:00 完成核验。当前 Fedora 44 KDE/KOS Wayland 宿主，内核 `7.2.11-200.fc44.x86_64`，rootless Podman 5.8.9。三套发行版使用原锁定镜像，各自用 APT、DNF、完整 `pacman -Syu` 安装软件。

三套环境的原生 Wayland GUI、双向剪贴板、保存及环境停止后的重开均通过。实际播放流分别到达当前 LinkBuds Open 蓝牙输出，采集的自身流 PCM 含预期音调；用户已回复“能听到”。用户的实听回复补充这一轮提示音验收，各环境的独立路径另由 PCM 和硬件路由证据确认。

| 环境 | 实际编辑器包 | 原生 Wayland | 双向剪贴板 | 保存/停机/重开 | 实际音频 |
|---|---|---|---|---|---|
| debian13 | `mousepad	0.6.3-1` | 通过 | 通过 | 通过 | 523 Hz，PCM 通过 |
| fedora44 | `mousepad	0.7.0-1.fc44.x86_64` | 通过 | 通过 | 通过 | 659 Hz，PCM 通过 |
| arch | `mousepad 0.7.0-1` | 通过 | 通过 | 通过 | 784 Hz，PCM 通过 |

使用真实 Mousepad 窗口，移除 `DISPLAY` 并强制 GTK Wayland。通过应用自己的 `edit.paste`、`edit.copy`、`file.save` 等 GAction 操作真实缓冲区；先确认保存前磁盘仍是原文，再核对中文 UTF-8 字节、SHA-256 和 `1000:1000` 属主。停止整个环境后重新打开，复制缓冲区回宿主再次核对。KWin 对象类型为 `KWin::XdgToplevelWindow`，客户端日志有 `xdg_toplevel` 与实际 buffer attach；没有用环境变量单独推定后端。[GTK 官方后端说明](https://docs.gtk.org/gtk3/wayland.html)。

- debian13：[重开窗口](debian13/native-attempt-2/reopened.png)、[Wayland/剪贴板结果](debian13/native-attempt-2/result.json)、[音频](debian13/native-attempt-2/audio.json)。
- fedora44：[重开窗口](fedora44/native-attempt-1/reopened.png)、[Wayland/剪贴板结果](fedora44/native-attempt-1/result.json)、[音频](fedora44/audio-retest-2/audio.json)。
- arch：[重开窗口](arch/native-attempt-1/reopened.png)、[Wayland/剪贴板结果](arch/native-attempt-1/result.json)、[音频](arch/audio-retest-1/audio.json)。

音频为低幅度三秒单音，容器内 `paplay` 实际播放。宿主用 `parec --monitor-stream` 只采本次 sink-input；无麦克风或其它应用音频采集。采样率 48 kHz、16-bit 单声道，预期频率能量占比约 1，RMS 约 1555，捕获 131072–196608 字节。输出为实际 `bluez5` 设备、RUNNING、未静音，设备音量 50%。没有修改全局输出、音量或静音。剪贴板原有 MIME 数据仅在内存备份和恢复，完整私有内容未写入证据；恢复检查通过。[实听回复](human-listening.json)。

## 腾讯会议

原始文件 `/home/omg/下载/TencentMeeting_0300000000_3.26.10.401_x86_64_default.publish.officialwebsite.deb`，197268516 字节，SHA-256 `70f37b029209c0ef91be9813eebf6650c6cb9a00f7c718490158773e2fe6acaf`。原生 APT/dpkg 安装 `wemeet 3.26.10.401 amd64` 成功，并在修复后的独立 Debian 环境中连续两次显示完整登录界面；每次后均停止整个环境，第二次为重新打开。未登录、未接受协议、未加入会议。

- [原始包信息](tencent-meeting/keep-id/package.json)、[安装事务](tencent-meeting/keep-id/install-transaction.json)、[当前启动配置](tencent-meeting/keep-id/launch-record.json)。
- [第一次完整登录界面](tencent-meeting/keep-id/launch-attempt-5/startup.png)、[结果](tencent-meeting/keep-id/launch-attempt-5/result.json)。
- [停机后重开界面](tencent-meeting/keep-id/launch-attempt-6/startup.png)、[结果](tencent-meeting/keep-id/launch-attempt-6/result.json)。两张登录截图已逐张查看，字节比较一致。

实际后端为 `KWin::X11Window`。厂商原始启动脚本在 Wayland 会话中选择 `xcb`/XWayland。本次使用普通 UID/GID 1000、独立 D-Bus、Mesa/Qt 软件渲染配置，保留原始启动脚本与 deb。没有 GPU 硬件直通；登录后的麦克风、摄像头、通话、视频和屏幕共享仍未验收。

启动配置为 `/usr/bin/dbus-run-session -- /opt/wemeet/wemeetapp.sh`，环境含 `LIBGL_ALWAYS_SOFTWARE=1`、`QT_OPENGL=software`、`QT_QUICK_BACKEND=software`、`QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu`。[Qt 官方软件渲染参数](https://doc.qt.io/qt-6.5/qtwebengine-features.html)。未设置 `--no-sandbox`、`QTWEBENGINE_DISABLE_SANDBOX` 或 privileged。

## 修复与保留的失败记录

1. 初次 KWin 检查误用不属于 Window 的 `waylandClient` 属性，已改用实际窗口类型和客户端协议日志；Debian 第一次检查失败保留，第二次完整通过。
2. Fedora/Arch 初次音频采样在新输出流尚未对应到设备列表时触发 `StopIteration`。已等待本次流绑定到可枚举、未静音且未 cork 的输出，再采 PCM，并加入四项回归。两者独立复测通过；抽出等待函数后的最终工具又在 Fedora 现场复测通过。第一次失败未记录瞬时 sink_ref，不能进一步断言该瞬间的服务端字段。
3. deb 的声明依赖未覆盖实际图形运行库和安装辅助工具。补齐 Wayland/GL/EGL、Xtst、字体、ALSA/Harfbuzz/ICE/SM、中文 locale、xdg-utils、procps 等包，再执行厂商 postinst，退出码 0。[原始缺库列表](tencent-meeting/ldd-before-repair.txt)、[修复后 ldd](tencent-meeting/keep-id/ldd.txt)、[postinst](tencent-meeting/keep-id/vendor-reconfigure.json)。
4. Chromium 拒绝容器内 root。产品新增可选 `env create --userns keep-id` 与 `app add --as-host-user`，包命令仍为容器 root，GUI 使用宿主 UID/GID。账户主目录与 `HOME` 同为容器内 `/home/multilinux-1000`；现有试验环境的错误 passwd home 已单独修复。新建环境的映射、主目录、XAUTH 可读性及共享文件属主均有[独立现场契约](keep-id-live-contract.json)，临时契约环境随后删除。[Podman 官方 UID 映射说明](https://docs.podman.io/en/latest/markdown/podman-run.1.html#userns-mode)。
5. 普通用户启动还遇到 D-Bus 自动启动工具缺失和图形初始化问题。系统调用记录有 `dbus-launch` ENOENT；独立 D-Bus 配合软件渲染后达到登录界面。普通用户及音频连通性见[身份检查](tencent-meeting/keep-id/identity-check.json)，图形/系统调用诊断保存在 `tencent-meeting/keep-id/diagnostics/`。启动窗口替换导致的一次截图被拒绝并删除，驱动增加等待稳定窗口及有界重试。

## 复查与保留状态

保留本轮原生三环境、腾讯会议 root 失败基线和修复后的普通用户环境，全部停止。原 `m3debian` 与上一轮三套 gVim 环境保持停止；宿主 2521 个 RPM 的清单 SHA-256 仍为 `26f4ba6fffc1c574fac87020e39a96ca31bb3e77c97022ab478128224ab1f27b`，桌面入口名单、MIME 配置和主项目环境/启动记录均未改变。

原生三环境使用原始 `project/` 快照。普通用户腾讯会议使用更新后的 `user-project/`，其第一版源码完整备份在 `tencent-meeting/keep-id/source-v1/`。初始快照、更新快照和当前源码分别核对，没有将旧实验绑定到新的源码结论。驱动、辅助协议客户端和新增测试另存 [verification-sources](verification-sources/manifest.json)。

打开已保留的腾讯会议：

```bash
python3 '/home/omg/文档/多兼容linux机/evidence/wayland-media-2026-10-22T082942+0800/user-project/tools/ml.py' app open meeting-wemeet-dbus-soft --
```

关闭应用后停止环境：

```bash
python3 '/home/omg/文档/多兼容linux机/evidence/wayland-media-2026-10-22T082942+0800/user-project/tools/ml.py' env stop meeting-debian13
```

复跑本轮独立验收：

```bash
python3 tools/probe_wayland_media.py prepare
python3 tools/probe_wayland_media.py native --run-dir <新证据目录>
python3 tools/probe_tencent_meeting.py --help
```

新登录会话的认证路径可能不同，需要创建新环境。腾讯会议重建时应按本轮原生包清单安装依赖、生成中文 locale，再使用 `--as-host-user --private-dbus --software-rendering`；原始 deb 只有在 Debian 环境实际测试，没有转换到 RPM/Arch 包。

[代码回归](regression.log)共 62 项，58 项通过、4 项旧现场集成测试默认跳过。本轮新图形、剪贴板、播放和腾讯会议流程由独立工具现场执行。[机器核验](validation.json)、[总记录](manifest.json)。这是一台 Fedora 宿主上的有界功能验收，人工鼠标键盘、跨宿主、GPU、完整 init/服务和真实会议功能仍不在通过范围。
