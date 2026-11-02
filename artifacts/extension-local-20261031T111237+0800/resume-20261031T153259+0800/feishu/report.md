# 飞书 Debian 本机启动阻塞修复与产品入口验证

日期：2026-10-31；状态：`WINDOW_VERIFIED_ACCOUNT_BLOCKED`。

## 结论

飞书 Debian 包本身安装成功，但启动时缺少运行库 `libasound.so.2`。`libframe.so` 的 dlopen 失败后，飞书在 `lark_ex_main_aura.cc(347)` 主动 SIGTRAP，shell 看到退出码 133。这不是 sandbox、Wayland/X11、UID 或产品入口参数问题。

在仅限本轮 Debian 测试客体内安装 `libasound2t64` 后，用产品公开 CLI 新建独立 `feishu-fixed` 登记，并通过 `bin/unifier app open feishu-fixed` 成功启动。主控独立读取精确 X11 client，得到 1648×940 的真实飞书 “Log In With QR Code” 登录界面，状态为 `WINDOW_VERIFIED_ACCOUNT_BLOCKED`。

## 根因与证据

先前失败日志只有 `Trace/breakpoint trap (core dumped)` / `guest_exit:133`，没有有效 stderr。廉价排查顺序如下：

1. 检查启动器：`/usr/bin/bytedance-feishu-stable` 指向 `/opt/bytedance/feishu/bytedance-feishu`，这是 bash wrapper；最终 ELF 是 `/opt/bytedance/feishu/feishu`，不是 Qt。
2. 直接运行 ELF 仍 SIGTRAP；`ldd feishu` 本身正常。
3. `strace` 显示 `libframe.so` 已 open，随后所有 `libasound.so.2` 搜索路径都是 `ENOENT`，最后 `SIGTRAP {si_code=SI_KERNEL}`。关键行见 [`strace-libasound-failure.txt`](strace-libasound-failure.txt)。
4. `gdb` 停在 SIGTRAP 时，栈上的 ASCII 直接给出：
   `... FATAL:lark_ex_main_aura.cc(347)] open /opt/bytedance/feishu/libframe.so, error libasound.so.2: cannot open shared object file: No such file or directory`
   见 [`gdb-sigtrap.txt`](gdb-sigtrap.txt)。
5. 修复后 `ldd /opt/bytedance/feishu/libframe.so` 已解析 `libasound.so.2`，见 [`libframe-ldd.txt`](libframe-ldd.txt)。

因此 App 的退出 133 是库加载失败后的主动崩溃，不是普通用户身份或显示后端拒绝。

## 变更

只改本轮测试 fixture，不改产品源码：

- 在 Debian 测试客体安装 `libasound2t64 1.2.14-1+deb13u1`，连带 `libasound2-data 1.2.14-1+deb13u1`、`alsa-ucm-conf 1.2.14-1`、`alsa-topology-conf 1.2.5.1-3`。原始安装日志见 [`apt-libasound2t64.log`](apt-libasound2t64.log)。
- 排查依赖：`strace 6.13+ds-1`、`gdb 16.3-1`、`file 1:5.46-5`，见 [`apt-debug-tools.log`](apt-debug-tools.log)。
- 正常关闭尝试需要发送 `WM_DELETE_WINDOW`，安装 `xdotool 1:3.20160805.1-5.1`（含 `libxdo3`），见 [`apt-xdotool.log`](apt-xdotool.log)。
- 实际包版本清单：[`debug-and-audio-packages.tsv`](debug-and-audio-packages.tsv)。
- 经产品 CLI 新建 `feishu-fixed`，保留旧 `feishu` 登记原件。旧登记 SHA-256 修改前后相同：
  `44fa8b69fbe95cd3862202d776e1d47ca1823db7dc66bef9f301f32db33e1ffd`。
  见 [`feishu-launch-before.sha256`](feishu-launch-before.sha256)、[`feishu-launch-after.sha256`](feishu-launch-after.sha256)。
- 新登记：[`feishu-fixed-launch.json`](feishu-fixed-launch.json)。argv 只有 `/usr/bin/bytedance-feishu-stable`，没有 `--no-sandbox` 或其他诊断参数。

容器身份始终是本轮 Debian 测试客体：

`076f6017d2a47a3fd6e2c996d6fb819c7c8ec2159d0fbb436be2a6e4bbd0e5d7`

没有 stop/remove 该容器，也没有碰两个日常容器。收尾时客体仍为 `running`。

## 产品入口与身份

实际入口：

```bash
UNIFIER_HOME=/home/omg/文档/多兼容linux机/.lab/extension-local-20261031/environments \
  bin/unifier app open feishu-fixed
```

实际执行的 guest argv 为：

```text
podman exec --user 1000:1000 \
  --workdir /home/omg/文档/多兼容linux机/.lab/extension-local-20261031/shared-project \
  --env ... --env HOME=/home/multilinux-1000 --env USER=omg --env LOGNAME=omg \
  ml-104d8e92-debian13 /usr/bin/bytedance-feishu-stable
```

- UID/GID：`1000:1000`
- HOME：`/home/multilinux-1000`
- 真实 CID：`076f6017...d0e5d7`
- 包：`bytedance-feishu-stable 7.72.23-0`
- wrapper：`/usr/bin/bytedance-feishu-stable` → `/opt/bytedance/feishu/bytedance-feishu`
- 最终 ELF：`/opt/bytedance/feishu/feishu`
- 运行时依赖：`libasound.so.2` 已由 `libasound2t64` 提供

身份和实际 argv 回执见 [`product-entry-facts.txt`](product-entry-facts.txt)、[`elf-and-package-identity.txt`](elf-and-package-identity.txt)。

## 窗口证据

主控独立读取精确 X11 client `0x2400004`：

- `_NET_WM_PID=12085`
- `WM_CLASS=Bytedance-feishu`
- `podman top` 确认 guest PID 12085 对应 host PID 781016，属于上述 CID
- 1648×940，真实 Feishu Log In With QR Code 界面
- 截图私有路径：`.lab/agents/extension-local-20261031T111237+0800/feishu-owned-client.png`
- 截图 SHA-256：`1e974157d3b464df91e9ab475b4f21e9a1e08a2341bbc45a53b47c4d0adc80c6`

完整回执见 [`primary-window-verification.json`](primary-window-verification.json)。截图只捕获该应用 X11 client，不声称 KWin compositor focus 成功；含登录 QR 的截图不放入 Git。

KWin 6.7.5 观察工具出现了缺口：`loadScript` 后只调用 `Script.run()` 没有回调；临时调用 `Scripting.start()` 后才返回窗口。`spectacle` 路径也超时，所以未采用该工具截图。临时工具改动已还原，记录见 [`kwin-helper-gap.txt`](kwin-helper-gap.txt)，不阻塞本轮启动验收。

## 关闭行为

尝试对已核验窗口做正常关闭：用 `xdotool windowclose 37748740`（即 `0x2400004`）发送 X11 `WM_DELETE_WINDOW`，返回 0。窗口从 X11 树消失，但飞书主进程、monitor 和 backend 仍存活，产品 launcher/worker 也继续持有 lease。结论：窗口关闭只把应用留在后台/托盘，不能记为正常退出通过。

随后按精确身份清理：guest 主 PID `12404`，start stamp `34949342`，实际命令 `/usr/bin/bytedance-feishu-stable`，host PID `792485`，同一 CID。只对该主 PID 发送 `TERM`。结果：

- 所有飞书 guest 进程退出；
- 产品 launcher `791599` 退出；
- `debian13` lease 恢复为 `{}`；
- 测试容器仍 `running`，未 stop/remove。

完整回执见 [`normal-close-before.txt`](normal-close-before.txt)、[`normal-close-state.txt`](normal-close-state.txt)、[`forced-cleanup-before.txt`](forced-cleanup-before.txt)、[`forced-cleanup-after.txt`](forced-cleanup-after.txt)。本次退出标记为 `FORCED_TEST_CLEANUP`，不是正常退出通过。

## 未测范围

- 无真实账号登录；登录二维码已过期，未扫码。
- 附件、消息、协作、会议、音视频、屏幕共享、文件保存/重开均未测，不能由“窗口出现”外推。
- 没有使用 `--no-sandbox`；没有关闭签名/安全设置，没有改宿主包数据库。
- 没有创建新容器，没有停止/删除测试客体，没有动日常 FreeCAD/腾讯会议容器。
- 未改产品源码、未改 execution-plan/README/HANDOFF，未 git 暂存/提交。

完整未裁剪的 strace 和原始直跑日志保留在私有控制目录
`.lab/agents/extension-local-20261031T111237+0800/feishu-diag-private/`，不复制进 Git 可见证据；
本目录只保留与根因和生命周期有关的小型摘录。

## 证据索引

| 文件 | 内容 |
| --- | --- |
| `gdb-sigtrap.txt` | SIGTRAP 栈与 `libasound.so.2` FATAL 文本 |
| `strace-libasound-failure.txt` | libframe/libasound 失败与 SIGTRAP 关键行 |
| `apt-libasound2t64.log` | 运行库原始安装日志 |
| `apt-debug-tools.log` | strace/gdb/file 安装日志 |
| `apt-xdotool.log` | 关闭测试工具安装日志 |
| `debug-and-audio-packages.tsv` | 实际包版本 |
| `libframe-ldd.txt` | 修复后动态库解析 |
| `elf-and-package-identity.txt` | wrapper/ELF/包身份 |
| `feishu-fixed-launch.json` | 新产品登记 |
| `feishu-launch-before/after.sha256` | 旧登记哈希不变 |
| `primary-window-verification.json` | 主控窗口验收回执 |
| `normal-close-*.txt` | WM_DELETE_WINDOW 尝试 |
| `forced-cleanup-*.txt` | 定向 TERM 清理与容器保持运行 |
| `product-entry-facts.txt` | 产品 argv、UID/HOME/CID、无 sandbox flag |
