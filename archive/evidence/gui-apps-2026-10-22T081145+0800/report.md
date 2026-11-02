# 三环境真实图形应用实测

时间：2026-10-22T08:11:45+08:00 至 2026-10-22T08:20:23+08:00。

测试在一台 Fedora 宿主的当前 Wayland 会话进行，三套环境串行使用各自仓库的 gVim。窗口通过 GTK3/X11 与 XWayland 显示，由 gVim 自身的客户端接口自动编辑和保存。

| 环境 | 原生包 | 结果 | 证据 |
|---|---|---|---|
| debian13 | vim-gtk3, fonts-noto-cjk | 通过 | [记录](debian13/attempt-1/result.json) / [重开窗口](debian13/attempt-1/reopened.png) |
| fedora44 | vim-X11, google-noto-sans-cjk-vf-fonts | 通过 | [记录](fedora44/attempt-1/result.json) / [重开窗口](fedora44/attempt-1/reopened.png) |
| arch | gvim, noto-fonts-cjk | 通过 | [记录](arch/attempt-1/result.json) / [重开窗口](arch/attempt-1/reopened.png) |

每套流程检查宿主原始文件读取、保存前缓冲区修改与磁盘未变、UTF-8 精确保存、宿主 UID/GID、正常退出、整个环境停止，以及重新启动后的缓冲区内容和文件 SHA-256。路径含中文和空格，内容含中文与引号。截图只捕获编辑器窗口。

环境与启动记录保留在独立源码快照中，流程结束后停止。再次打开：

```bash
python3 tools/probe_desktop_apps.py open --run-dir '/home/omg/文档/多兼容linux机/evidence/gui-apps-2026-10-22T081145+0800' --target debian13
python3 tools/probe_desktop_apps.py open --run-dir '/home/omg/文档/多兼容linux机/evidence/gui-apps-2026-10-22T081145+0800' --target fedora44
python3 tools/probe_desktop_apps.py open --run-dir '/home/omg/文档/多兼容linux机/evidence/gui-apps-2026-10-22T081145+0800' --target arch
```

当前会话结束后认证路径可能失效，届时需创建新一轮环境。旧项目环境保持原状。
本轮不覆盖人工鼠标键盘交互、原生 Wayland GUI、剪贴板、音频播放、GPU、完整 init/服务、跨宿主或性能排名。资源快照分别记录 cgroup 和 PSS，不相加。

原生包名参考 [Fedora 官方包页](https://packages.fedoraproject.org/pkgs/vim/vim-X11/) 和 [Arch 官方包页](https://archlinux.org/packages/extra/x86_64/gvim/)，实际安装版本以每环境包清单为准。

[机器可读总记录](manifest.json)包含源码校验、镜像身份、事务和宿主隔离核对。

最终核查见 [validation.json](validation.json)：三套环境当前均为停止状态；原生事务、源文件哈希、保存/重开文件、六张截图、资源记录和报告链接通过核对。三张重开截图已逐一查看，中文与全部四行内容显示正常。
