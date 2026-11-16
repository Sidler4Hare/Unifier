# 钉钉上传窗口配置

活动窗口的 `WM_WINDOW_ROLE` 为 `GtkFileChooserDialog`，X11 窗口 PID 与客体内钉钉主进程的 PID 一致。宿主 KDE 文件选择器没有接管这个窗口。DingTalk 菜单仍使用 Unifier v0.0.4 r3 和原 Debian 容器。

- 在客体 GTK 书签加入「Unifier 共享文件」，目标是已核验的宿主数据绑定。
- 只在钉钉的启动记录中选择 Breeze Dark GTK 样式和独立的 FileChooser 设置。GLib 已读取并验证默认路径输入框、文件夹优先和默认隐藏点文件。Breeze Dark 样式及资源与宿主已安装的同名主题文件逐字节一致。
- 钉钉启动配置使用客体已生成的 `zh_CN.UTF-8`，并让 GTK 通过 XIM 使用宿主已有的 Fcitx。客体有 Noto CJK 和 XIM 模块，GTK3 简体中文消息目录缺失。实际输入法切换、显示效果和响应时间需要单独核验。

最初的离线运行未满足压力准入。第二次运行验证了 GLib 设置，发现浅色主题的深色入口引用另外一个主题。修正后的整体验证再次未满足压力准入。生产配置改用系统完整的 Breeze Dark 主题，未采用有外部引用的入口文件。

启动记录的原始字节和应用配置变更后的校验和保存在 `~/.local/state/unifier/rollback/dingtalk-gtk-picker-20261110/`。首轮和后续离线回执分别在 `offline/`、`offline-retry/` 和 `offline-final/`。
