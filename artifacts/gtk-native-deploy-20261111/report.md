# 日常 r5 部署与钉钉 GTK Native Portal 验收

2026-11-11。**部署成功；人工冷启动的宿主 Portal 打开与取消已通过；完整文件上传未通过。** 用户随后报告上传失败，已发现所选宿主文件在客体路径中不存在，原生 Portal 的文件交付/路径映射仍未实现，不能把本次结果称为上传功能完全修复。

## 部署

日常 ~/.local/bin/unifier 从 r4 切至 `0.0.4-wt20261111-c947077f-dirty-r5`。原钉钉桌面入口经项目 tools/unifier.py 动态解析日常入口，继续使用原管理根。r5 只合入 `file_chooser.py`、`native_library_launcher.py`、`service.py` 三个产品文件，保留 r4 其他修复。针对候选安装版重新运行 37 项定向检查，全部通过、无跳过。

通过产品 setter 把钉钉设为 `gtk-native-portal`，启动记录仅该模式字段变化。备份和条件回退脚本位于 `/home/omg/.local/state/unifier/rollback/gtk-native-20261111T175523+0800/`。其中 restore.py 在当前入口/记录仍属于这次部署时恢复 r4 和旧设置，不终止应用。新进程需重启才应用回退。

旧主进程 PID1508127/start7466233 及登记后代已核验消失。主进程 TERM 后未退出，重新核对身份并通过 pidfd KILL；无按名字清理、无直接停止共享容器。产品租约自动收尾后原 CID exited，之后才由用户从原入口启动。

## 实际调用链

证据 `observation-1/trace.jsonl`，时刻为同一单调时钟：

| 时刻 | 证据 |
| --- | --- |
| 21698.55 | 记录器就绪，未发现钉钉进程 |
| 21907.93 | 新主进程 PID2373670/start8287534，客体 PID18，CEF 指纹匹配 |
| 21907.93 | 新进程 GTK_USE_PORTAL=1，LD_LIBRARY_PATH 为客体私有兼容目录 |
| 21908.71 | 经当前代理 PID2373159 的 CEF Portal 探测成功，FileChooser version=4 |
| 21918.0386 | 客户端 :1.373438 经代理 PID2373159 调用 org.freedesktop.portal.FileChooser.OpenFile |
| 21918.0407 | 宿主 Portal PID2292/:1.69 调用 KDE 后端 :1.100 的 org.freedesktop.impl.portal.FileChooser.OpenFile |
| 21918.0913 | 钉钉 native_loader_attempted=1，六个 Native 函数指针和库句柄全部非零 |
| 21930.2657 | KDE 后端返回 status=1，无 D-Bus 错误 |
| 21930.2667 | 宿主 Portal 向原客户端发送同一请求路径的 Response(status=1)，即取消 |
| 21938.6048 | 记录器正常结束 |

请求路径为 `/org/freedesktop/portal/desktop/request/1_373438/gtk2052184704`，客户端、宿主转发及取消响应匹配。接收后端的名字所有者 :1.100 对应 PID2412 `/usr/libexec/xdg-desktop-portal-kde`。每个目标客户端消息均通过当前 lease 中代理 PID/start/CID 关联，不将其他应用的 Portal 请求计入。

收尾再次核验代理 PID2373159/start 与当前 meeting lease 一致；客体私有别名 `libgtk-3.so` 指向 `/usr/lib/x86_64-linux-gnu/libgtk-3.so.0.2417.32`，缓存目录权限0700。日常 r5 三文件哈希未变。CEF 标准 chooser 缓存仍为0符合预期：这次通过修复后的钉钉 GTK Native 分支走 Portal，而不是改用 CEF/KDialog。

## 用户实际上传失败

用户报告打开后尝试实际上传提示失败，并提供宿主文件：
`/home/omg/文档/实验课/操作系统/实验二/实验二(24计算机5班穆思远).doc`

只读取元数据，没有读取或发送该文件内容：宿主文件存在、大小930304字节；在同一新钉钉进程 `/proc/2373670/root` 下查询相同路径返回 ENOENT。该路径不在已登记/实际挂载的数据共享内。当前唯一数据共享为：
`/home/omg/.local/share/multilinux/documents/wemeet` → 客体同路径。

详见 `upload-path-diagnosis.json`。本轮取消调用链证明窗口转发成功，但不覆盖用户之后的文件选择/上传调用；没有保存该次选择响应中的 URI 或文件读取 syscall，不能声称取得完整失败上传的逐调用链。已确认的功能缺口是：原生 GTK Portal 返回路径不经过 kdialog 助手的映射；当前实现只支持已同路径共享的文件，宿主任意文件不能因此自动进入容器。

后续完整修复需要把 Portal 选择结果接入逐文件授权与客体可达路径交付，而不是继续修改 GTK/KDE 选择条件。没有擅自共享整个宿主家目录、复制用户文件或将实际上传失败改称通过。本轮保留 r5 和新模式，便于继续补齐文件交付能力。

## 收尾与边界

准备、候选检查、部署、旧进程停止、观察器五个受保护作业均退出0、reason=null，工作cgroup及私有临时目录均已移除；见 final-audit.json。该文件中的 PASS 只对应标题所述的 cold-start OpenFile and cancellation，不代表用户随后反馈的实际上传成功。

容器总数仍11，各实例状态与本轮起点相同；日常钉钉与原容器保持运行。原先获准的 wayland-0→wayland-1 临时链接未改变。没有新建容器、包事务、宿主全局设置变化、桌面输入自动化、聊天读取、提交或推送。

观察器预算只覆盖记录器，不覆盖用户手动运行的日常应用；本轮是人工工作流观察，不算自动T3/PRE03验收。用户报告的上传失败保留为未完成项。
