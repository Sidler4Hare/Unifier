# 2026-11-01 扩测缺陷修复与 v0.0.2 部署

本轮完成前次复核确认的包事务异常恢复、终止清理和安装版本漂移，并修复 APT 内层维护脚本的终端缩放。新包已部署到全局入口及 FreeCAD、腾讯会议的既有入口。新增功能与未测工作流没有据此标为通过。

## 已修复

1. **超时后的包清单与日志。** 原生命令开始前保存事务、argv、执行状态和环境绑定；Podman 就绪后保存真实客体 PID/start/session/group。异常保留输出尾部、退出及清理信息。确认执行结束后重新读取清单；包集变化会更新摘要和内容版本。无法证明结束或读取失败时标记 unknown/needs-recovery，拒绝下一次包修改。
2. **终止及整个进程树。** SIGTERM/HUP/INT 进入受控清理并恢复终端与原 signal handler。真实 APT 测试发现 dpkg 的内层 PTY 可以产生独立 session，因此补充 PPID 子树发现、冻结、PID/start 核验、TERM/CONT、必要时 KILL 及消失核验。取消只作用于绑定 CID 中本次执行的子树，不依靠代理退出判断成功。
3. **崩溃恢复。** 新增 `unifier pkg recover ID`。进行中日志在 SIGKILL 后仍阻止重入；`env inspect` 显示未知清单和待恢复事务。原生数据库未配置完成时继续拒绝恢复，不偷偷执行厂商脚本。成功停止精确容器会持久记录旧执行已结束，因此允许用户重新启动做原生修复，然后重核清单；原事务保持失败并记录恢复时间。
4. **APT 缩放。** 交互 apt-get 加 `Dpkg::Use-Pty=0`，保留确认和输入，让 dpkg 维护脚本获得变化后的尺寸；非交互参数保持原样。
5. **日常交付。** 上述修复和 10 月 31 日的自动补库、原生提示、z/Z 停止视图、缺失环境状态、项目终端及包文件名修复，统一进入 v0.0.2。

## 验证

| 验证 | 结果与边界 |
| --- | --- |
| 独立解包检查 | 616 项，611 通过、5 条件跳过；语法、旧名转发、五入口、清单检查通过。见 `../unifier-0.0.2.tar.gz.validation.log`。 |
| 定向进程清理 | 本地真实 TERM/HUP/INT、超时、输出保留、handler 恢复，以及另一个 session 中的后代均有回归。 |
| Debian 真实 SIGTERM | 在自制测试包 postinst 等待时终止控制器，退出 143；精确进程树消失、无遗留数据库写锁，清单版本改变；半配置状态触发 needs-recovery，下一次包修改被拒绝。 |
| Debian 恢复与包生命周期 | 审计拒绝半配置数据库；只对自制 fixture 修复其脚本并运行原生配置后 recover 成功。随后本地包旧→新、卸载和重装通过。见 [native-attempt3/native-result.json](native-attempt3/native-result.json)。 |
| 真实 APT/dpkg PTY | postinst 提示后将外层 24×80 改为 38×120，输入继续后脚本实际记录 `38 120`；不是只有代理收到 SIGWINCH。原始终端输出在 `native-attempt3/apt-resize.terminal.log`。 |
| Debian 真实 SIGKILL | 控制器退出 -9，进行中日志仍在；inspect 显示 unknown，重入和运行态恢复被拒绝；明确停止→启动并修复 fixture→recover→停止通过。见 [crash-final/result.json](crash-final/result.json)。 |
| 安装与回退 | 临时入口实际执行 0.0.1→0.0.2→0.0.1→0.0.2，卸载入口后数据保留；部署候选也实际退回旧日常入口一次，最终再部署修复后的构建。见 [install-rollback.json](install-rollback.json)。 |
| 部署与保全 | 全局入口、FreeCAD 和腾讯会议的四个旧/新名入口均为 0.0.2，原绑定预检通过。原有 8 个容器的 CID/Image/Mounts/完整 State 均未变；日常文件仅 4 个入口和 2 份部署元数据改变，Git index 字节未变。见 [preservation-audit.json](preservation-audit.json)。 |

中断测试只在独立容器 `ml-26942dd5-interrupt-debian`（CID `d8720702648d9246175c59e8e73abf9b5a153ed889ed35db662474faee74166b`）运行，已停止保留；没有向日常应用注入故障。当前总计 9 个停止容器，不能按旧“四个容器”的记录批量清理。

第一份自制 DEB 缺父目录，原始失败保留；随后的实机测试暴露跨 session 子进程和停止证明问题，均保留原回执并修复复验。`candidate1/` 是中间构建和回退记录，不是当前正式包。`native-attempt2/` 和 `crash/` 是补丁完善前的证据；最终成功回执为 `native-attempt3/`、`crash-final/`。

## 精确交付与回退

- 包：`artifacts/unifier-0.0.2.tar.gz`，217 项源码/链接清单。
- SHA-256：`b656bf73abfe126e2bddc27b3b184c494bf098631bc9d57e52ae83538b4e7856`。
- 冻结目录：`/home/omg/.local/share/unifier-code/releases/0.0.2-b656bf73abfe`。
- 部署回执：[deployment.json](deployment.json)。工作树与正式清单逐项相符；历史 v0.0.1 包和代码保留。
- 修前源码备份：`.lab/agents/expansion-fix-20261101/before-implementation.tar.gz`，SHA-256 `af5a4448e0284eadd09fa9347cfba469a77dad29e4258aeab803ee6a5c9f8aa5`。
- 原日常入口/元数据备份：`.lab/agents/expansion-fix-20261101/daily-entrypoints-before-final.tar.gz`。停止有关应用后，从仓库执行 `python3 artifacts/expansion-fix-20261101/deploy_release.py --rollback` 可恢复原入口和部署元数据；工具会先核对当前文件仍属于本次部署，拒绝覆盖后来改变的文件，不恢复或覆盖业务数据。

本轮未暂存、提交、推送或创建 PR。代码、测试与说明的本轮修改范围可由源码备份和 preservation-audit.json 复核，未将之前的工作树修改归因于本轮。

## 仍开放的扩大测试

五个闭源 GUI 单元的完整编辑/保存/停机重开、账号/媒体、自动补库后新客体的 GUI、客体原生 Agent 与受控桥、Fedora/DNF 新路径实机、watcher/ACL/xattr/递归树、通用 IDE 和长期负载仍未完成。这些是独立能力与验收工作，不由包恢复修复替代。账号、非 Fedora 桌面和远端执行也没有在本轮被伪造为可用。

前次机器汇总中旧 `feishu_debian=INSTALLED_NO_WINDOW` 字段不改写历史；本轮 [current-status.json](current-status.json) 给出当前权威状态，引用后来真实窗口回执，并继续区分自动补库客体的 runtime_loading=unverified。
