# FreeCAD 日常菜单登记

2026-10-24，本机用户级部署，采用已通过 B 阶段真实 GUI 验收的代码快照，以及本地已安装 FreeCAD 1.1.4 的固定 Arch 镜像。未安装宿主包。

菜单名称 **FreeCAD**，桌面文件 `/home/omg/.local/share/applications/unifier-freecad.desktop`。其 Exec 指向保留的 `/home/omg/.local/share/multilinux/apps/freecad/tools/ml.py`，不依赖会被清理的验收环境，也没有加载验收宏。图标来自这个容器中的 FreeCAD 安装。

工作目录为 `/home/omg/文档/Unifier-CAD`，这是独立登记的宿主项目与环境共享范围。应用私有 HOME 位于保留的容器中；通用 cwd 不能保证任意新建对话框的保存目录。菜单启动本次只验收启动、关闭和按需停止，保存行为沿用 B 阶段的有限验收边界。

`menu-validation.json` 记录两次 `gtk-launch unifier-freecad`，每次都观察真实 Wayland 主窗口，使用容器内进程对应的宿主 PID 和 KWin internalId 限定截图与关闭请求；窗口关闭后环境自动停止、应用租约归零，两次共用同一 CID。`menu-launch-2.png` 已人工查看，显示完整 FreeCAD 主界面。GLib DesktopAppInfo 能按桌面 ID 找到应用。

最终保留菜单、图标、应用、包、项目和停止的日常容器。原来其他桌面入口保持原样。本次仅登记菜单，不修改默认文件关联。

早期验收脚本分别遇到启动子进程继承 stdout 导致等待 EOF，以及过早接受初始化窗口；已改用普通日志文件并等待主窗口稳定。对应尝试单独保留，不作为最终成功证据。

如需撤销本次菜单入口，可在此独立应用目录运行 `python3 tools/ml.py app unexport freecad`。撤销菜单不会删除项目文件。
