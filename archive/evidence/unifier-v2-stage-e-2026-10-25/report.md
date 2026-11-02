# E 项目会话与可撤销入口：本机受限真实验收

2026-10-25，继续由 dsh Command Code `deepseek/deepseek-v4.1-flash` 实现，主控独立实测、补回归并修复最后一处退役终态漏列。**E 的本机受限闭环已通过；完整回归 359 项，355 通过、4 条件跳过。** D 的受控普通文件完整移动已先通过并独立备份，然后才派发 E。当前源代码和验证未提交/推送。

最终结果以 `acceptance-result.json`、`primary-final2-*`、`postconditions.json` 为准。`evidence-audit.json` 另记录 48 项证据/源码一致性检查，属于记录核对，不计作新增运行测试。早期失败和修正记录保留；native-sessions-attempt3 的功能用例虽通过，但当时源码仍在改动，不能作为最终验收。

| 验收 | 真实结果 |
|---|---|
| Debian / Fedora / Arch 项目执行 | 三套新隔离 keep-id 环境；项目计划不启动容器；解释器与子进程均属指定 guest，绝对链接按 guest 解析；中文/空格/引号/分号/百分号参数保持单个 argv |
| 管道与状态 | stdin 中文管道、分离 stdout/stderr、exit 23；三套缺 Python 与宿主独有程序时均 127，无宿主回退；项目新文件 UID 为宿主 1000 |
| 共享会话取消 | 两个会话同 CID/同 StartedAt、两个租约；只取消本次 guest，SIGTERM=143，另一个继续完成，最后关闭自动停止 |
| 普通退出与后台子进程 | 手动预启动环境被保留；自愿 exit 15 仍为 15；shell 后台子进程结束前被覆盖或清理，不在租约释放后失控继续 |
| 崩溃与恢复 | launcher SIGKILL 后 detached worker 继续完成并清理；worker 和宿主代理都强杀后，活 guest 的 unresolved 租约保留，另一个会话退出不误停；显式定向 cancel 后确认清理，恢复两次 |
| 真实终端 | 实际 PTY 中进入一次项目 shell 后直接运行命令，Ctrl-C 中断 sleep 返回 130，shell 仍可执行下一命令；exit 17 与 termios 恢复通过 |
| 原生桌面关联 | 隔离 XDG 中由真实 GLib/GIO 解析默认项与启动器；文件名准确传入；同名 app 重建时旧入口拒绝；撤销后原默认与原始 mimeapps 字节恢复 |
| 缓存、外部修改与撤销窗口 | 已撤销的旧缓存 launcher 不启动应用；后来的用户选择在冲突中保留；4 个撤销 intent/逐文件 undo 边界真实 SIGKILL，恢复两次后恢复原字节且不重新授权 |
| C/D 组合回归 | 同一不变 E 源码重新通过 C 三发行版只读 FUSE/生命周期/缺失后端、C→D 复制，D 35 项受控移动、7 项日志窗口、20 项旧复制、真实 ENOSPC、新旧 CLI |
| 日常 FreeCAD | 更新为已验证的 E 源码，旧代码独立备份，配置/状态/桌面入口原样保留；再次实际菜单启动两次，真 Wayland 主窗口、正常关闭/自动停止，同一日常 CID |
| 清理与保全 | 6 个本轮测试容器和 3 个卷已精确删除；E 的 3 个环境走正常 remove/live-session guard，未绕过。原有 27 容器 ID/名称/最终状态、23 个主状态文件、13,572 个此前证据文件及 B/C/D 清单保持不变；无本轮 FUSE/storage 残留 |

## 日常入口

先用已有 `root add`、`project add --root ROOT_UUID --environment ENV_ID --alias NAME` 显式接受项目/环境绑定，然后：

```bash
python3 tools/unifier.py project run NAME -- command argument
python3 tools/unifier.py project shell NAME
# 进入 shell 后直接使用命令，整条工具链处于同一个已接受环境
python3 tools/unifier.py project session list
python3 tools/unifier.py project session cancel SESSION_UUID --signal TERM
python3 tools/unifier.py project session recover SESSION_UUID

python3 tools/unifier.py project export NAME --menu-only --terminal
python3 tools/unifier.py project export NAME --app APP_ID --mime-type application/x-multilinux-demo --extension mldemo
python3 tools/unifier.py project revoke INTEGRATION_UUID
python3 tools/unifier.py project integration recover
```

D 的完整普通文件移动仍要求源根 `--cleanup-policy controlled` 与本次 `file move --cleanup-source` 两项显式授权。默认 move 和旧待清理 journal 保留源，不自动继承删除权，详见 `../unifier-v2-stage-d-complete-2026-10-25/report.md`。

## 本轮抓到并修复的边界

直接杀宿主 podman exec 代理不能证明 guest 已停止，原始反例保存在 `proxy-signal-mechanism.json`。实现改为独立控制 FD 上报 guest pid/start/session，按同一文档身份在 guest 内复核、定向发信号、确认整个本次执行结束。所用 `-i`、PTY 和 FD 能力见 [Podman exec 官方文档](https://docs.podman.io/en/latest/markdown/podman-exec.1.html)。

首次执行生成 UID 524288 文件的失败保留；最终固定 keep-id 文档身份 1000:1000，并用于执行、probe、signal、cleanup，不用 chown 补偿。自愿退出码和取消信号分开。已确认 gone/无租约的 interrupted 记录作为历史结束证明，允许正常退役；未知 guest 继续保留 unresolved。

桌面 launcher 固定 integration/project/environment/app-instance/fingerprint，启动前、detached worker 和最终 exec 再校验；旧缓存和重名新应用不继承授权。撤销 intent 持久化后，恢复只完成撤销；revoked/conflict 不因旧字节重新出现而恢复 installed。格式和默认关联语义参考 [Desktop Entry Exec](https://specifications.freedesktop.org/desktop-entry/latest/exec-variables.html) 与 [MIME 默认关联](https://specifications.freedesktop.org/mime-apps/latest/default.html)。

## 范围、重跑与恢复点

本轮原生矩阵限本机 rootless Podman 5.8.9、crun、overlay、同路径已授权共享与三套 keep-id guest。rootless-default 身份规则有单元契约测试，不能扩写为本轮原生项目会话矩阵。guest 依赖 sh、/proc，非 TTY 需 setsid --wait；能力/终止无法证明时保守拒绝或保留 unresolved。整个环境的静态共享范围仍由所有应用共用，不是逐文件/逐应用沙箱。

未实现全局宿主命令拦截、自动换发行版重试、任意后台 detach；不宣称任意安装窗口自动恢复、跨系统原子事务、完整 GUI Save As 适配、压力/性能或跨宿主验收。混合安装/未知外部修改保持冲突。桌面原生证明使用隔离 XDG，并只为被启动进程恢复真实 Podman 存储环境；原用户默认应用未改。日常 FreeCAD 的现有真实菜单另做两次兼容验证，未把它算作新项目菜单面板可视交互验收。F 尚未开始。

`prepare_containers.py`、`probe_native_sessions.py`、`probe_native_desktop.py`、`probe_desktop_revoke_windows.py` 和 `run_acceptance.py` 保留复跑步骤。旧测试容器已删除，须在新隔离证据/项目目录准备新 fixture；不要复用已删除 CID 或重写旧报告。`--reset-settled-sessions` 仅在证明这组实验 guest 与宿主 worker 都已结束后备份/重置其临时会话，绝不操作主项目状态。

检查点及备份 SHA256 见 `.lab/agents/unifier-v2-stage-e-20261025/completed.json`，D 的独立检查点继续保留。源码、测试、配置、设计与阶段证据可恢复；私有日志/凭据/套接字/缓存不进入阶段源码备份。
