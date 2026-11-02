# 复杂 Arch 包现场验证

2026-10-22 20:23 起（Asia/Shanghai），Fedora 44 KDE/KOS 宿主，内核 `7.2.11-200.fc44.x86_64`。独立 `complex-arch` 环境，从原锁定 Arch 镜像创建，完整 `pacman -Syu`，普通用户 UID/GID 1000:1000，原生 Wayland，允许当前 GPU 设备。生产模块没有修改。

**结论：FreeCAD 的有界真实 GUI 工作流通过；Sunshine 的 GitHub 原生包可安装、命令入口可运行，但系统集成未完成，并发现事务状态不能体现维护钩子失败。**

## FreeCAD

- [GitHub 上游](https://github.com/FreeCAD/FreeCAD)，安装的是 [Arch 官方原生包](https://archlinux.org/packages/extra/x86_64/freecad/)，不是 GitHub AppImage。实测 FreeCAD 1.1.4、revision 45040，实际包版本见 `packages-final.txt`。
- 真实 Wayland 窗口：应用报告 Qt platform `wayland`，KWin 观察到 `XdgToplevelWindow`。两轮截图人工检查了模型、孔、立体视角和中文名称。
- 建立 40×30×20 方块减圆柱的参数模型，把孔半径从 5 改成 6，重算后体积从 22429.203673205102 变为 21738.05328941535。解析预期为 `24000 - π×36×20`，误差小于 1e-6；一实体、形状有效。
- 保存 `work/中文 模型.FCStd`，导出 STEP 后重新解析，实体数、有效性和体积一致。
- 正常退出并停止整个环境后再次打开，参数、体积和 STEP 回读均一致。最终两次应用启动器退出码均为 0，重开前后 FCStd SHA-256 不变，文件均归宿主用户。
- 图形显示不等于证明硬件加速：本轮没有对实际 FreeCAD 渲染器做 GPU/软件渲染归因，没有性能排名或人工鼠标操作验收。

证据：`gui-results.json`、`files.json`、`build-window.png`、`reopen-window.png`、`work/*-result.json`、`install.json`。

初始测量保留：`attempt1-welcome-page/` 的数学与重开检查通过，但欢迎页盖住模型、缺中文字体；`attempt2-close-dialog/` 已显示正确模型，关闭时触发未保存视图状态提示，探针超时并清理环境。最终补装容器内 `noto-fonts-cjk`，等待启动完成、选择模型页，并在已经保存几何模型后通过 FreeCAD API 关闭文档再退出。不把探针的修正前尝试算作最终通过。

## Sunshine：直接来自 GitHub 的 Arch 包

[官方 Release v2026.914.233613](https://github.com/LizardByte/Sunshine/releases/tag/v2026.914.233613) 的 `sunshine-2026.914.233613-1-x86_64.pkg.tar.zst`，9,445,660 字节。

SHA-256：`17eb8c4d823a2693038f40aeeab56943ef0d5d6ace8ac90b3693905b0acbba87`，与 GitHub Release asset digest 一致。下载包留在本地 `downloads/`，该目录忽略二进制；下载 URL 在 `sources.json`。安装前读取 `.PKGINFO` 和 `.INSTALL`。

1. `pkg route` 正确识别 Arch；经项目本地安装接口执行完整 `pacman -Syu` 后 `pacman -U`，没有绕过依赖或关闭签名检查。
2. 默认镜像连续两次发生超时/TLS 下载失败，包括依赖签名文件；项目两次都记为 `failed/native-command`，Sunshine 未入库，失败日志保留。为这套实验环境增加 [Arch 官方列出的 USTC 镜像](https://archlinux.org/mirrors/ustc.edu.cn/656/) 后成功。前后 mirrorlist 有独立副本，宿主镜像配置未改。
3. 最终 `sunshine --version` 返回 0；另外以容器内普通用户 1000:1000 执行也返回正常版本。`ldd` 未报告缺失动态库，文件 capabilities 为 `cap_sys_admin,cap_sys_nice=p`；文件 capabilities 不代表进程在宿主拥有这些权限。
4. **维护脚本仍失败**：udev 对 sysfs 写入 `Permission denied`，`modprobe uinput/uhid` 找不到宿主内核模块目录，systemd 模块加载钩子报告 PID 1 不是 systemd；`pacman` stderr 有两条 `command failed to execute correctly`，但原生命令退出 0，项目最终记为 `completed`。
5. `/dev/uinput`、`/dev/uhid` 在环境内不存在。未启动串流服务、未配对 Moonlight、未验收捕获/编码/输入注入/音频链路。不得据此称 Sunshine 完全兼容。

证据：`sunshine-install*.json`、`sunshine-PKGINFO.txt`、`sunshine-INSTALL.txt`、`sunshine-user-version.txt`、`sunshine-runtime.txt`、`pacman.log`。

## 新发现与下一步

优先修复/补测：

1. 已知生命周期缺陷：PID 1 信号和僵尸回收、强杀启动器后租约所有权、停止中快速重开。原启动性能报告中的问题本轮没有修复。
2. 包事务结果增加“原生包入库 / 命令可运行 / 维护钩子警告 / 系统集成未验证”的分层状态。不能只根据 pacman 退出 0 和 `--version` 推导应用可用，也不应把所有含 error 的文本简单当作包安装失败。
3. 在有明确宿主适配设计后验证 Sunshine 设备与服务能力；不要把容器改成 privileged 作为通过条件。
4. 下一类复杂应用建议 OBS/Kdenlive：项目保存、视频渲染、音频同步、连续采集与源断开恢复。候选源码和测试矩阵见仓库 `调研/复杂Arch应用与后续测试.md`。

## 回收与复现

- 两个最终 FreeCAD 窗口已关闭，实验环境停止；镜像、安装后的环境与模型保留。原腾讯会议和其它环境未操作。
- 宿主包清单、用户桌面入口名单和原项目环境/启动记录前后相同，见 `validation.json`、`host-after.json` 和 `manifest.json`。这不是宿主所有文件的完整差异审计。
- 105 项代码回归，101 通过、4 项旧现场测试按设计跳过；这些回归不覆盖掉维护钩子和生命周期缺陷。
- 初始 CLI 有一次 `--use-command --version` 参数写法错误，未进入事务；修正为 `--use-command=--version`。宿主无 bsdtar，改用 GNU tar 只读提取包元数据，未安装宿主工具。
- `project/` 保存实际使用的产品代码和配置；两份探针源码单独保存，原始尝试没有覆写。可用快照 CLI 的 `env inspect complex-arch` 核对现场。`tools/probe_arch_complex.py --run-dir <新准备的实验目录>` 要求已安装 FreeCAD、共享 `work/` 和宏；拒绝覆盖已有结果。

本轮仅涵盖这台 Fedora 宿主。AUR 源码构建、其它 GPU、长期运行、完整 systemd 和跨宿主 M5 均未验收。
