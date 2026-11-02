# Unifier v0.0.1 修复与测试交付

完成于 2026-10-31，Asia/Shanghai；源码与首轮实机运行冻结于 2026-10-30。本版交付范围为 CLI、跨派系容器管理、普通共享文件及有明确边界的停止环境只读视图，可进入限定范围内测。

## 交付与安装

- 测试包：`../unifier-0.0.1.tar.gz`；SHA-256：`b463bddede2f6f1b12d469369d74a1fb8eef9df752fd59fc3b6b6eb72b8e497c`。
- 包内及旁边有 207 项源文件/链接的清单。正式包由 `tools/build_release.py` 生成，独立解包、清单核验和完整离线检查通过后才落盘，拒绝覆盖旧包。
- 安装、升级、回退和反馈说明在 `../../docs/v0.0.1.md`。`unifier --version` 为 `Unifier 0.0.1`。
- 本机冻结代码目录：`~/.local/share/unifier-code/releases/0.0.1-b463bddede2f`。全局启动器和两款日常应用的 `tools/ml.py`、`tools/unifier.py` 均接到此包；日常应用继续使用原来的数据根和绑定。部署回执为 `deployment.json`。
- 此轮没有创建 Git 提交、标签、PR 或远端仓库 Release。原暂存条目保持一致；发布包包含当时完整工作树，精确身份取清单和包摘要。

## 修复内容

1. Python 3.11/3.12 没有导出 `fcntl.F_SETOWN_EX` 时，使用 Linux UAPI 的同一操作常量，继续获取线程定向租约并恢复信号掩码。真实 Ubuntu Python 3.12 上受管移动与故障恢复回归通过；没有降级删除保护。
2. 停止视图列举目录时，可跳过无法跟随的可选链接目标（EACCES/EPERM/EOPNOTSUPP）；直接访问受限目标仍报错。I/O、FD 耗尽等错误保留，普通可读兄弟目录不受 `/etc/mtab -> /proc/mounts` 阻断。
3. 普通文件每次属性重验证都使页面缓存失效，解决 size/mtime/ctime 碰撞导致漏判的问题。保留 mmap 和旧句柄的对象身份。没有后台 watcher；仅重复读已有 mmap、没有任何属性重验证时，不承诺外部修改即时可见。保守失效有缓存成本，未作性能收益结论。
4. 当前已有的 rename 路径上下文修复补上真实 FUSE 验收：相对链接、嵌套挂载归属、旧 FD/mmap 都有对应实测。
5. 新增版本输出、无 Git 源码包清单检查、独立解包构建、显式升级与入口回退备份。未知启动器仍拒绝覆盖。
6. 真 GPU DMA-BUF 用例归入显式 `MULTILINUX_INTEGRATION=1`。离线检查不再因存在 render 节点便默认要求 GPU 能力。原 Arch 虚拟 GPU `gbm_alloc` unsupported 记录保留，GPU 能力仍未验收。

Linux 常量依据本机 `/usr/include/asm-generic/fcntl.h`；只读页面失效的调用边界参照 [libfuse 通知 API](https://libfuse.github.io/doxygen/fuse__lowlevel_8h.html)。KWin 观测工具的能力依据 [KWin scripting API](https://develop.kde.org/docs/plasma/kwin/api/)；本轮实际运行结果单独记录。

## 当前源码包的验证

| 验证 | 结果与实际范围 |
| --- | --- |
| 本机 Fedora 独立解包检查 | Ran 575 tests；OK (skipped=5)，语法、旧包名转发和五个 CLI 入口通过。日志为 `../unifier-0.0.1.tar.gz.validation.log` |
| Ubuntu 实体机 / Python 3.12.3 | Ran 562 tests；OK (skipped=6)；32 个实机驱动步骤通过 |
| Arch 虚拟机 / Python 3.14.8 | Ran 575 tests；OK (skipped=5)；32 个实机驱动步骤通过 |
| Debian 虚拟机 / Python 3.13.5 | Ran 562 tests；OK (skipped=7)；32 个实机驱动步骤通过 |
| 真实 FUSE | 本机及三台远端各 5 组通过；每机 50 次快速改写后 pread/mmap 正确，正常卸载、worker 返回 0 |
| 安装 / 入口切换 / 回退 / 卸载入口 | 两份独立包副本实际执行通过，保留数据字节；未知链接拒绝覆盖。见 `install-rollback.json` |
| 本机部署 | 冻结包清单校验、两款真实应用绑定、腾讯会议桌面集成全部 pin、四个应用入口版本及全局入口通过；见 `deployment.json` |

不同宿主原生构建依赖和条件测试不同，测试总数不直接互减；跳过项不算真实功能通过。最低 Python 3.11 本轮没有独立运行时验收，缺失常量回归和真实 3.12 结果单列。

三台远端始终使用同一个正式包，传输时逐机校验包摘要，执行前核对包内源清单。回执压缩包取回后再次校验摘要，原始文件在 `remote/<宿主>/`；三机收集记录为 `remote/collection.json`。

六个正向组合为 Ubuntu → Fedora/Arch、Debian → Fedora/Arch、Arch → Debian/Fedora。每个组合覆盖真实客体身份、共享宿主内核、原生包查询、退出码、同 CID 重开、CLI 初始化、专用目录互写、运行与停止状态目录浏览。每台宿主还验证同派系初始化/创建拒绝。原有容器的 CID、状态和启停时间均保持原样；本轮临时容器与挂载清理完成。

Debian 的快速改写实挂测试出现 40/50 次原始 ctime 碰撞，重验证后的 pread 和 mmap 均正确；其余三机本轮碰撞数为 0，确定性离线用例另外强制模拟完全相同的元数据。不是依靠增加等待时间使旧失败消失。

## 应用验证

复用原 Arch FreeCAD 1.1.4，以正式包 CLI/worker 启动：建立带孔模型、参数重算、保存中文文件名 FCStd、导出 STEP、正常关闭，再启动并重开模型与 STEP。模型和 STEP 均为一个有效实体，体积与解析值一致；两次正常退出并自动停止同一 CID。Qt 报告 `wayland`、visible、exposed，客户端完整截图已查看。原应用登记字节保持一致。

产物在 `freecad-workflow/`，原合成模型保留在独立的 `Unifier-CAD/v0.0.1-acceptance-20261031T101657/`。这是已安装应用的真实 GUI 保存/重开流程，未重新安装 FreeCAD。没有把这项验证推广为全部客体和全部应用验收。

首次 KWin 截图探针没有返回结果，原失败与 FreeCAD 构建成功结果保留在 `freecad/` 和 `freecad-driver.log`。后续使用 Qt 客户端截图和窗口 exposed 状态完成应用流程；该结果不冒充 KWin 合成器截屏。腾讯会议完成了新版入口和绑定预检，本轮没有进行实际会议、设备或媒体验收。

## 保全、失败与回退

修前备份：`/home/omg/文档/Unifier-backups/20261030-224546-before-v0.0.1-fixes/`。204 个源码/链接/index 项已逐项校验；包含原暂存、未暂存补丁。另有日常状态与正式部署前入口/元数据备份。

- 原 17 个日常状态文件中 16 个字节不变；FreeCAD 环境记录只更新 `updated_at`。两款应用 UUID、generation、CID 不变，原桌面项和应用记录保留。
- 全局入口旧目标保留在 `~/.local/bin/.unifier-gprm8yd4.rollback`；应用入口和部署元数据回退用备份 `release-entrypoints-before.tar.gz`。回退只恢复对应入口/元数据，不覆盖之后产生的状态或业务文件。
- `changes.patch` 是相对本轮修前快照的补丁，不包含之前已经存在的未暂存工作；原 Git 暂存条目未变化。
- 第一次实挂发现实际受限错误为 EOPNOTSUPP，失败保留在 `live-files/`，修复后在 `live-files-2/` 及三宿主复验通过。
- 两份中间构建包以 `candidate1-*`、`candidate2-*` 保留；它们不是当前正式包。

服务器专用 SSH 密钥保存在本机 `~/.ssh/id_ed25519_unifier_server`，既有配置有独立备份；原 LAN 入口为 `unifier-server`，外网入口为 `unifier-server-remote`。远程通过既有 Tailscale 网络，按其要求由用户完成网页登录验证；没有保存登录密码或修改 Tailscale 策略。

## 仍待后续验收的能力

v0.0.1 内测重点是 CLI 和已测文件范围。新应用安装闭环、WPS/Zoom/飞书扩大矩阵、全部客体的 GUI、真实 Agent 跨环境桥接、宿主 RPM/Flatpak handler、媒体设备、虚拟 GPU、通用 IDE、严格快照、递归树与 ACL/xattr 全保真、长时间资源/性能以及断电恢复分别保留未完成状态。完整扩大测试不因本轮通过变为全部完成。
