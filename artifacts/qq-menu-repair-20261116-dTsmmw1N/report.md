# QQ 菜单恢复与依赖扫描修复

原有 Debian 13 `meeting` 容器中已安装 `linuxqq`。开销较小的标准 `app add` 验证了原入口 `/opt/QQ/qq`，随后用 `app export --menu-only` 生成菜单。没有重新安装 QQ。原安装事务的 SHA-256 校验一致。

- 已安装的 `ipc.node` 真实 ELF 数据可由修正后的扫描器读取。原 DEB 的 35 个常规 ELF 文件全部通过扫描，识别出四个以 `.node` 结尾的 SONAME。[实机文件扫描](real-ipc-scan.json) · [原包扫描](archive-scan-t1/command.log)
- KDE 菜单生成包含 `multilinux-6b061a2f-qq.desktop`，显示名为 QQ。人工从应用列表启动后，KDE 创建了对应桌面服务单元，Unifier 创建 QQ 运行租约，XWayland 报告 `WM_CLASS "qq", "QQ"`，窗口 PID 对应容器内 QQ。[菜单](kde-menu.log) · [KDE 单元](qq-launcher-unit.log) · [QQ 窗口](qq-window.log)
- 通用修复把 ELF SONAME/NEEDED 解读与仓库查询规则分开。可确认安装且无恢复障碍的应用在扫描失败时仍会登记菜单。失败诊断继续由安装命令报告。两个定向套件合计 79 项通过，独立解包回归 1069 项通过、5 项条件跳过。[回归](build-t1/command.log) · [补丁包回归](unifier-0.0.7+wayland.3.tar.gz.validation.log)
- 日用及开发树均已修正。日用入口是 `0.0.7+wayland.3`。已验证的 QQ 菜单保留原启动命令，扫描和安装后的登记使用更新的日用入口。[部署](deployment.json)

回退到上一版日用代码时，在本回执目录运行 `python3 -B deploy.py --restore`。已有 QQ 菜单独立保留。
