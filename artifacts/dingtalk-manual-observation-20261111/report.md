# 首次人工上传观察

状态 PARTIAL。用户手动操作后旧主进程 526714（starttime 1015282，客体 PID 18）仍存活，新进程 1406316 短暂出现后退出，窗口属于旧主进程。因此本次不是冷启动，不能作为冷启动链路证据。

`upload-open-snapshot.json` 同步确认：窗口 0x26001e8 的角色 GtkFileChooserDialog、PID 18、类名 DingTalk；同一主进程实际 libcef 的标准选择器缓存值 0，Portal available/complete 均为 1。由实际二进制的选择分支反汇编可知，此窗口没有通过那个标准 Portal/KDialog/toolkit 选择入口；不能解释为该入口探测失败后缓存 GTK。

总线监听部分不完整：首版 Gio filter 将监控消息继续交给普通消息派发，存在自动响应并导致监控连接断开的缺陷；收尾 close_sync 报连接已关闭，原生退出 1。不得用缺少 OpenFile 日志证明没有调用。进程标量和窗口身份的独立观察仍有有效快照。回执 reason=null，无资源保护中止，工作 cgroup 和临时目录已清理。

监听器已修正为在 BecomeMonitor 完成后安装消费型 filter，不将监控流交给普通 dispatcher；增加连接断开记录。`../guard-tuning-20261111/monitor-probe/` 用私有总线运行实际修正版，验证两轮真实 NameHasOwner 调用/响应均被记录、未断连、原生退出 0。

用户随后明确要求直接杀钉钉。`authorized-stop.json` 记录：核验完整 CID、PID/starttime 和 executable 后，先 SIGTERM，10 秒后仍未退出，重新核验并通过 pidfd 向同一进程发送 SIGKILL。主进程及已登记子进程已消失。没有广域 kill 或直接 podman stop；随后观察到产品自行将容器恢复 exited。

此证据保留原失败。下一轮单独记录真正冷启动，不覆盖本目录。
