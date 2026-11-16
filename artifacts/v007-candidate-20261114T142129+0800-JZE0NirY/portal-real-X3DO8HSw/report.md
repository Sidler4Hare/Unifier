# 0.0.7.dev0 真实应用文件授权验收

**PASS。** 2026-11-14 15:50（北京时间）。使用升回退已验证的同一份 0.0.7.dev0 候选，SHA-256 `7f99118f0a34d10df73b8f94624ce3119679f8fc3e11acb4d218f7aa5f1a4a2b`。实际桥接进程从该包的解包目录导入模块，结束后复核包及全部 326 项清单。

## 真实使用及撤销

1. Debian 13 中的 Zenity 4.1.90-1 通过 GTK4 调用私有桌面的 XDG Portal `OpenFile`。在真实 GTK 文件选择器里确认一个未挂载的宿主临时文本。
2. 候选桥给出 55 字节交付副本。Zenity 文本查看器实际打开并显示唯一标记，桥接读事件、查看器输出与宿主源文件的 SHA-256 一致。
3. 查看器窗口及 PID/starttime 保持存活。用候选的正式 `unifier file access revoke`，账本 `revoking → released`。客体同 UID 的真实 `open()` 对完全相同的交付路径返回 `ENOENT`（errno 2）。候选创建的副本已经回收，宿主原文件哈希保持一致。
4. 正常确认并关闭 Zenity，应用退出码 0，容器随最后一个租约结束自动停止。撤销验证范围是交付副本路径的新打开，应用已经读入的文本仍可显示。

[机器结果](workflow-r6/workflow.json) · [真实查看器截图](workflow-r6/viewer.png) · [桥接事件](workflow-r6/bridge-audit.jsonl) · [撤销命令](workflow-r6/revoke-command.json) · [释放账本](workflow-r6/released-grant.json) · [重开失败](workflow-r6/reopen.json)

## 装置及收尾

只从本地固定镜像创建 1 个 CID `8935ee29407a4943e529eac5c7346a61e16e559a30aec6c46b396110327beea2`，保留为停止态。客体无外部网络，IPC/PID 命名空间独立，只绑定本轮专用 Portal 目录。没有安装软件包。显示走本轮私有 Xvfb、认证 Unix 转发与 GTK Cairo 软件绘制。只有核验后的 Podman 进程使用用户 systemd 控制总线，Portal 和应用使用私有总线。

早期尝试的镜像能力预检、Podman 控制总线与 X11 驱动问题均单独留档，第五轮功能通过但截图全黑，第六轮的 Cairo 截图与应用输出均已核验。全程无保护中止。第六轮最低宿主可用内存约 2.99 GiB，作业峰值约 331 MiB、113 任务。监督器清理自身控制工作单元后工作单元标签为 failed，原始命令及独立监督均退出 0，工作单元 MainPID 0，cgroup 为空。所有登记进程、桥接、显示端点和临时目录已收尾，应用租约为零。11 个既有实例的状态和启动时间保持一致，非目标启动/执行事件未出现。日用入口仍为 0.0.6，Git 索引、boot ID 和 KWin 身份复核一致。

[保护回执](workflow-r6/receipt.json) · [单元终态](workflow-r6/units-after.txt) · [最终保全](final-verification.json) · [环境记录](final-fixture-state.json) · [执行记录](execution.md)
