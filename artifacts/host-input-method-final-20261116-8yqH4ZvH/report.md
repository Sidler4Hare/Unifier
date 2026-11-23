# 宿主输入法补丁

之前的 QQ 设置验证了宿主 Fcitx 经本机 XWayland 的 XIM 路径。补丁把相同机制接到 Unifier 的通用应用执行入口。

- 启动前核对应用登记、实际 Podman 挂载、当前宿主 `DISPLAY` 和本机 X11 socket。验证成功后传递宿主 `XMODIFIERS`。
- 默认 `auto` 只在能确认使用 X11 时选择 GTK XIM。两个显示协议都可用且后端未明确时，保留应用自己的 GTK 模块选择。
- `unifier app input-method <应用ID> host-xim` 为已使用 X11/XWayland 的应用启用 GTK XIM。它沿用原来的显示协议、输入区域设置和总线权限。`native` 关闭通用输入法接入，`auto` 恢复自动规则。应在该应用的管理根运行命令。
- 输入法偏好只选择已有 X11 通道上的工具包前端。已有应用执行记录和带身份校验的导出入口可继续使用。
- QQ 的菜单使用既有日用启动器。此前为 QQ 设置的 Fcitx 环境和字符区域仍可用于从补丁回退后的启动。

回退日用代码可在本回执目录执行 `python3 -B deploy.py --restore`。保留旧版本目录和原 QQ 菜单文本。

## 验证

- 日用版已部署为 `0.0.7+wayland.4`，开发树包含同一修复。[部署](deployment.json)
- 定向回归 4 项通过。独立解包回归 1073 项通过，5 项条件跳过。[定向回归](build-t1/command.log) · [解包回归](unifier-0.0.7+wayland.4.tar.gz.validation.log)
- QQ 菜单通过 KDE 菜单生成检查，新版入口实证可连接 Fedora 宿主的 Fcitx XIM。Mousepad 经相同产品入口实际收到 `XMODIFIERS`，在双显示环境下沿用自己的 GTK 模块。后者未实测输入法候选词和上屏。[实机检查](verified-live.log)
