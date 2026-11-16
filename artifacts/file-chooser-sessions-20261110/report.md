# 钉钉选择器拆解与 GNOME、无头环境兼容

## 钉钉 8.3.1

宿主进程 10175 在 Debian 客体 PID 命名空间中是 18。当前上传窗口的 `WM_WINDOW_ROLE` 是 `GtkFileChooserDialog`，窗口报告 PID 18。进程装载了钉钉自带的 `libdt_web_view.so`、`libcef.so` 和 Debian 的 GTK3 库。

在本机 `libdt_web_view.so`（SHA-256 `9b001fb4b7a668c5998bc048ef1f3e107d16cd3392e3a2ad4bf8b451113d23c0`）中，`client::RootWindow::OnFileDialog` 从 ELF 虚拟地址 `0x205cd0` 开始。它在 `0x205ce4` 检查回调字段，字段为空时跳到返回 false 的路径。回调函数指针等于默认的 `dt::IWebViewDialogHandler::OnWebViewFileDialog` 时也保留 false。自定义函数在 `0x2060df` 通过虚调用执行，返回值由 `0x2060e2` 保留并传回 CEF。CEF 的公开契约约定未接管时使用默认选择器。钉钉内置 Chromium 109 的默认 Linux 选择顺序是 Portal、适用时的 KDialog、工具包选择器。[CEF 回调契约](https://raw.githubusercontent.com/chromiumembedded/cef/5414/include/cef_dialog_handler.h) · [Chromium 109 选择顺序](https://raw.githubusercontent.com/chromium/chromium/109.0.5414.120/ui/shell_dialogs/shell_dialog_linux.cc)

静态证据证明厂商回调可以接管，实机证据证明本次最终创建了 GTK3 窗口。本轮没有记录该次回调的返回值或 CEF 启动时的 Portal 探测，因此不能确定本次是否经过厂商自定义分支。后续需要在隔离会话里记录从进程启动到首次上传的 D-Bus 请求，检查 `FileChooser.OpenFile` 和 KDialog 子进程，再用测试文件核对返回路径能否在客体打开。

## Unifier 实现

- `zenity-portal` 接住 `zenity --file-selection`。支持单选、多选、目录、另存、筛选、父窗口和模态标记。Zenity 的分隔符只用于输出，路径内含同一分隔符时整批拒绝。继续复用已有的 FileChooser 和共享路径核验。宿主自行选择 GNOME、KDE 或其他配置好的 Portal 后端。[Zenity 文件选择参数](https://raw.githubusercontent.com/GNOME/zenity/master/help/C/file-selection.page)
- 图形转发启动前检查宿主会话总线及本地 Wayland 或 X11 socket。仅有用户总线的无头环境会在启动客体和代理前得到明确错误。客体执行尚未提交的错误允许安全释放租约。
- `unifier app add <客体> <应用> --exec <客体程序> --headless --on-demand` 注册无桌面应用。既有 `unifier file open --path <宿主路径>` 继续通过登记的文件根、项目和处理器把共享原文件传给程序。该路径依赖明确的文件参数和已批准的共享绑定。

代码保存在 `codex/auto-host-share`。日常版尚未部署本批改动，真实钉钉接管与真实 GNOME 桌面仍待实测。

保护器下 115 项定向回归通过。私有 D-Bus 探针执行实际发布的 KDialog 和 Zenity 辅助程序，验证了文件与目录选择、中文文件名、多选分隔符、另存、取消和拒绝未共享路径。无头用例以临时原文件及 FakeRuntime 验证登记路径传入 CLI 应用。首轮新增测试中两个上下文管理器的书写错误已修正，原始失败留在 `acceptance/`，通过回执在 `acceptance-fixed/`。保护器已清理工作 cgroup 和临时目录。
