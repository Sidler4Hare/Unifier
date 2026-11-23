# 钉钉冷启动至首次上传框：绕过标准选择器

2026-11-11。结论：**本次上传窗口绕过了 CEF 内含的标准 Portal/KDialog/toolkit 选择入口；不是该入口探测失败后回退 GTK。** 这是用户操作下的真实工作流观察，已确定两种候选原因中的类别；没有动态函数调用栈，不声称已经定位到厂商私有回调的具体调用行。

## 冷启动及调用证据

旧主进程和子进程已经按用户明确授权结束；本轮开始时原日常容器 exited，未发现钉钉主进程。用户从原启动入口启动后，新主进程宿主 PID **1508127**、starttime **7466233**、客体 PID **18**。实际 libcef.so SHA-256 为 `cd808083f2b8df1f01e98494ec6b7ac5a04bd63c971191a199f63d03fcca4441`，与反汇编所用文件相同。进程只有这一份 libcef 映射。

`trace.jsonl` 的单调时间顺序：

| 时刻 | 观察 |
| --- | --- |
| 13631.3138 | 观察器就绪；进程列表为空，随后才由用户启动 |
| 13695.3970 | 新钉钉指纹核验；choice=0、portal_available=0、portal_complete=0 |
| 13700.0901–13700.0909 | NameHasOwner(org.freedesktop.portal.Desktop) 返回 true，无错误 |
| 13700.0914–13700.0919 | Properties.Get(FileChooser version) 返回 **4**，无错误 |
| 13700.1627 | 同一钉钉进程 portal_available=1、portal_complete=1，choice 仍为 0 |
| 13719.3425 | 窗口 **0x260000c** 出现，WM_WINDOW_ROLE=GtkFileChooserDialog、WM_CLASS=DingTalk、窗口 PID=18 |
| 13871.6049 | 观察器到期正常结束，无断连或过滤异常 |

观察期间未记录到 `org.freedesktop.portal.FileChooser.OpenFile`。`result.json` 进一步将探测请求发送者 `:1.60846` 的宿主 PID 解析为 **1507589**，与原管理根当前 lease 的 Portal 代理 PID 一致，避免将其他应用的探测误认作本次钉钉请求。

进程状态每 250 ms 采样、窗口身份每秒采样，仅状态变化落盘；GTK 上传框出现时选择器缓存没有变化。`live-snapshot.json` 在同一 PID/start 上再次只读核验，choice=0，available/complete 均为 1；启动环境的 PATH 含已发布的 chooser-kdialog 助手，XDG_CURRENT_DESKTOP=KDE、KDE_SESSION_VERSION=5。没有配置丢失或仍使用旧进程的问题。

## 为什么能够排除“探测失败回退”

本机实际二进制定位见 `../guard-tuning-20261111/cef-static/xref-2.asm`、`xref-3.asm`，不是仅凭上游版本号猜测：

- 标准入口 ELF VA 约 `0x7cfe0a0`，缓存变量 `0xc66e540`：0=尚未选择，3=Portal，2=KDialog，1=toolkit。
- 缓存为 0 时先读 Portal 可用标志；可用则写入 3。不可用才继续 KDE/KDialog 检查，最终回退 toolkit 会写入 1。
- 已观察到同一进程成功探测且 GTK 窗口出现，但缓存始终未离开 0；因此此次 GTK 窗口没有经过这个标准分派入口。

Unifier 的 `kdialog-portal` 实现是发布命令助手并设置 PATH/KDE 环境，只有应用执行 kdialog 才能转发；它没有拦截进程内 GTK 窗口创建。这解释了配置已经生效、Portal 也可用，上传框却仍使用 GTK。

**修复方向应转向钉钉/CEF 的实际文件对话框入口或 GTK 层适配。** 单纯继续设置 KDE 环境、换 GTK 主题或重启 Portal，不能覆盖已观察到的绕行路径。具体由哪个厂商回调/自定义工厂创建 GTK 尚未取得调用栈，不将它写成已证实函数名。也不能仅凭 250ms 轮询没看到短命 kdialog 就断言从未执行；本结论以内部状态、窗口归属与实际选择分支为主证据。

## 独立启动问题及临时恢复

真正冷启动首次失败发生在 crun：既有容器固定挂载 `/run/user/1000/wayland-0`，宿主仅有 wayland-1。详见前一轮 `../dingtalk-cold-observation-20261111/startup-error.log`。它发生在钉钉执行前，与 GTK 分派问题独立。

用户明确批准后，本轮新增了 `wayland-0 -> wayland-1` 临时符号链接，原 socket 保持不变，未重建容器或改写 Podman 数据库。链接身份在 `temporary-link.json`。链接暂时保留以便原容器后续冷启动；回退必须再次核验 inode/UID/目标，只删本轮链接，不删 socket。这不是会话路径兼容的永久产品修复。

## 收尾及验证边界

- 观察器原生退出 **0**，reason=null，工作 cgroup 和临时目录已移除；最低宿主可用约 **2.95 GiB**，观察器作业峰值约 **180.60 MiB**，没有保护中止。
- 没有发送文件、读取聊天正文/凭据/文件内容；只记录进程标量、窗口身份和 Portal 元数据。用户手动操作的日常应用不在观察器硬预算内，因此本轮不冒充 T3/PRE03 完整自动化验收。
- 用户的钉钉和原容器留在运行态，不再自动结束；没有新增或删除容器。Git HEAD 未变、暂存内容仍与 HEAD 一致；索引文件原始哈希有变化，不能声称字节不变，未执行暂存/提交/重置或恢复索引。产品代码未修复或部署。
- 本轮规范/保护器调整及 36 项合同通过见 `../guard-tuning-20261111/report.md`。全仓 891 项有 1 项请求桥恢复失败、5 项跳过，保留原失败，不宣称全仓通过。
- 首轮监听器缺陷及未真正退出旧进程的无效冷启动证据保留在 `../dingtalk-manual-observation-20261111/`，未覆盖或追溯改成通过。

## 后续反汇编补充

同日进一步定位到钉钉主库 `FileDialog::doModal → doModalUseGtk`，以及独立的 `loadGtk3`/`gtkNativeAvailable` 失败：它请求未提供的 `libgtk-3.so`，而已安装并加载的是 `libgtk-3.so.0`；当前进程尝试标志为 1、句柄和 Native 函数指针全为 0。因而本报告“不是探测失败”严格限定为 **CEF 的 Portal 探测没有失败**；钉钉自身的 GTK Native 加载确实失败。具体分支、状态、边界见 [反汇编报告](../dingtalk-disassembly-20261111/report.md)。
