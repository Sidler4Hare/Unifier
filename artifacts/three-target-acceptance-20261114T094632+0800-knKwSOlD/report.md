# Debian、Fedora、Arch 包事务负向与桌面验收

2026-11-14，北京时间。开发树 0.0.7.dev0。本机固定 Debian 13 实例、Ubuntu 固定 Fedora 44 与 Arch generation 1 实例的 **15 项实机用例全部通过**。执行范围和保护条件见 [执行记录](execution.md)，事务编号、原生退出码及回执见 [机器台账](case-ledger.json)。

| 用例 | Debian 13 | Fedora 44 | Arch |
|---|---|---|---|
| 仓库网络拒绝 | 私有 loopback 端口拒绝，APT 100 | 同一独立故障，DNF 1 | 私有同步数据库更新失败，pacman 1 |
| 签名拒绝 | 未签名仓库 Release 被 APT 拒绝，退出 100 | 强制检查本地 RPM，未签名包被 DNF 拒绝，退出 1 | 强制本地签名，未签名包被 pacman 拒绝，退出 1 |
| 维护脚本失败 | DEB preinst 47，APT 100，包未安装 | RPM %post 47，DNF 1；保留 rpm-scriptlet-failed，测试包随后正式卸载 | 签名包的 PreTransaction hook 失败，pacman 1；记录 pacman-hook-failed，包未安装 |
| 主动中断与恢复 | 捕获 APT GET 后 SIGINT，CLI 130；原生审计 0，正式 pkg recover 返回 no-recovery-needed | 捕获 DNF GET 后 SIGINT，CLI 130；原生审计 0，正式 pkg recover 返回 no-recovery-needed | 捕获 pacman GET 后 SIGINT，CLI 130；正式 pkg recover 返回 recovered，原生审计 0 |
| 桌面安装、菜单、启动、退出 | 合成 DEB，由私有应用列表导出的 desktop 文件经 GIO 启动真实 X11 窗口，WM_DELETE_WINDOW 后退出 0、实例自动停止 | 同等 RPM 流程通过 | 临时签名信任库下安装合成 Arch 包，同等菜单和窗口流程通过 |

三次主动中断均在测试仓库请求已到达、包数据库写入前发出，记录了 native exec 绑定及退出后的清扫证明。此前 Debian、Arch 的实际保护中止恢复另有历史回执。本轮 Fedora 没有在写入或脚本执行过程中主动中断。Arch 的签名拒绝和桌面本地包以独立配置单独验证 -U，历史官方签名本地包及全量同步已有完整正向回执。

## 修复与收尾

- Debian 的失败 preinst 留下合法的 `install ok not-installed` 选择记录。停止态读取器现在验证该记录并排除未安装项，最终原生查询、清单和登记摘要一致。
- 桌面导出的启动命令从当前代码入口启动，并明确传递管理根。Arch 包清单中的 `/usr/bin/` 目录被排除出应用候选。同一调用中发生外层异常时，已完成的包事务收尾仍能正确落账。
- 中间无效样本均留存。包括适配器参数、远端 RPM 私有依赖、显示工具和测试包准备错误、私有 XDG 改变 Podman 搜索位置、测试进程额外占用文件描述符以及本机 SELinux 拒绝跨域 Unix socket。Debian 最终通过只在既有测试共享目录内交换小文件的私有 X11 转发，Fedora 与 Arch 用认证 Unix socket 转发。Ubuntu 私有 Xvfb 副本只改用私有 xkbcomp 路径。显示未对外监听，未改系统 SELinux、宿主包或活动桌面。
- 三个实例恢复为停止、PID 0、应用租约和 bridge 空，包清单恢复原摘要。三组桌面峰值分别约 149、165、157 MiB 和 31、37、36 任务。最小宿主可用内存分别 3.15、115.32、115.32 GiB。本轮有效用例均无资源保护中止。固定实例 cgroup 与控制器已退出，无新增容器或非目标启动。此前 Debian 失败样本遗留的应用租约已通过正式 app recover 协调，清理事务的恢复屏障由正式 pkg recover 调和。
- 三个目标使用合成小应用、私有 XDG 软件列表和独立 X11 显示。测试不采用活动 KDE、账号或媒体设备。Arch 范围为 Ubuntu 上的 generation 1，本机 generation 2 不在这轮。没有发布、部署或提交，日常入口继续指向 v0.0.6。

最终 [T1 回归](t1-final/receipt.json) 执行 1068 项，1063 项通过、5 项条件跳过，命令退出 0。保留原 Python ResourceWarning，独立监督记录工作 cgroup 和临时目录已清除。Git 索引、HEAD 与日常 v0.0.6 入口保持原状。
