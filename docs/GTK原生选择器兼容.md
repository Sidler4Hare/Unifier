# GTK 原生文件选择器兼容

`gtk-native-portal` 是应用级、显式启用的文件选择模式。它针对应用通过无版本库名 `libgtk-3.so` 动态解析 GTK Native API、但客体只提供运行库 `libgtk-3.so.0` 的情况。它不识别或修改钉钉二进制，也不依赖钉钉固定地址；其他满足相同条件的 GTK 3 应用可以使用同一规则。

```sh
unifier app file-chooser <应用ID> gtk-native-portal
```

设置只更新该应用启动记录，不立即启动容器。模式变化会改变应用执行指纹；已绑定旧指纹的桌面集成须沿用现有流程重新登记。应用应完整退出后重新启动，否则已有进程缓存的失败结果不会消失。

通过 `unifier init --desktop`、`unifier configure --desktop` 和 `unifier env create --desktop` 新建的桌面环境默认启用管理式 Portal 桥接。这条规则适用于 Debian、Fedora、Arch 和后来加入的发行版，共用同一套桌面创建流程。新登记的按需图形应用默认使用 `gtk-portal`，识别到钉钉桌面条目或已知程序入口时使用 `gtk-native-portal`。识别到 FreeCAD 的 `org.freecad.FreeCAD` 桌面条目或 `/usr/bin/freecad` 入口时，默认使用 `qt-portal`。其他使用系统 Qt 平台主题的应用可以显式选择 `qt-portal`。新建时可选用 `--no-portals`。

已有桌面实例可用 `unifier env portal-bridge <环境ID>` 查询桥接设置。对缺少桥接的 rootless Podman 桌面实例，在应用退出并确认容器停止、租约已释放后，执行 `unifier env portal-bridge <环境ID> enable`。下次由 Unifier 启动时，为该实例挂载该管理根的私有桥接目录。随后按应用启用选择器模式。

回退时先将使用 Portal 的应用调回 `native`，再在停止态执行 `unifier env portal-bridge <环境ID> disable`。创建时由 `--portals` 固定的挂载沿用原配置。

启动时，Unifier 校验已有桌面 Portal 桥，启用原生 Portal 文件交付通道，再发布独立的 Python 启动辅助程序。辅助程序在客体内：

1. 通过客体动态加载器定位 `libgtk-3.so.0`，校验 ELF64/amd64、SONAME，以及六个 Native API 确实来自同一个库。
2. 在客体临时目录下的 `unifier-native-<uid>/<库身份摘要>` 私有目录建立 `libgtk-3.so` 别名。目录必须归当前客体用户所有、权限为 0700；不跟随被替换的目录链接，不覆盖冲突别名。
3. 仅在本次应用环境的 `LD_LIBRARY_PATH` 前加入该目录，保留已有搜索路径及其他变量；配合 `GTK_USE_PORTAL=1`，直接 exec 原始 argv。没有新增常驻包装进程，也不通过 shell 拼接参数。

这是一条明确允许的 GTK 3 映射，不是将任意缺失 `.so` 自动指向某个版本。它不安装开发包、不修改系统库目录、不复制宿主库、不打应用补丁。缺少 Python 3、GTK Native API 或校验失败时启动失败并给出错误，不静默回退普通 GTK 后宣称转发成功。

## 文件与后端边界

要求应用实际使用 GtkFileChooserNative；对完全写死 GtkFileChooserDialog 的程序没有强制接管作用。GTK Native 自身仍可能因特殊选项或 Portal 故障回退，是否真正到达宿主必须以 OpenFile/SaveFile 调用和用户工作流验证。

WPS Office 12.1.2.29183 默认由应用自己的窗口处理打开和另存为。整合模式和多组件模式都走这条路径。随包的 Qt 5.12 使用 `kso_qt` 命名空间，其平台主题和文件对话框 ABI 也有扩展。私有 Debian 上的专用适配已验证通过 KDE 打开、另存 DOCX 和 DOC、重开并正常退出。除了转发 Qt 对话框，适配还修正了该版 Writer 保存时的格式列表分隔符，否则选中 DOCX 后仍会写出旧版 Word 二进制。源码、重建参数和验收方法见 [WPS Writer 适配](../tools/wps_portal/README.md)。

Debian 13 的 `unifier install` 本地 WPS 安装路径提供厂商配置脚本所需的 `hexdump` 和实测缺少的系统库。新登记的图形入口在命令恰为 `/usr/bin/wps` 时使用 `stop-children`。主程序退出后，按需启动器回收这次启动拥有的云助手和 CEF 子进程，随后释放应用租约。

桌面应用安装会检查客体内是否已有可用的 sans-serif 字体。精简系统缺失时，会通过客体原生包管理器补齐 fontconfig 和该发行版的 DejaVu 基础字体。FreeCAD 在精简 Arch 上的菜单和属性标签已通过这条路径恢复可读。

FreeCAD 1.1.4-3 已在私有 Arch 桌面上从应用菜单启动，经 KDE 保存并重开 FCStd，再导出和导入 STEP。两个工作流都校验了实际几何尺寸和正常退出。保存目标位于已配对的可写共享。

`gtk-portal`、`gtk-native-portal`、`qt-portal` 共用原生 Portal 文件交付通道。它校验宿主 Portal 的成功返回，再把路径转换到已核验的实际共享；未共享的只读普通文件则生成保持原文件名的临时快照，放入既有桥接挂载。无需同路径共享或重建容器。快照只代表选择时的内容，不是可编辑的原件。

未共享的目录、可写选择和另存请求明确失败，须先配置对应共享。取消、失败、伪造响应和已经关闭的请求不会授权复制。每文件最多 512 MiB、每桥接租约最多 1 GiB、128 个成功批次，最多 256 个文件/批次，串行复制；磁盘保留至少 5 GiB 或文件系统的 10%。符号链接、多硬链接和非常规对象拒绝。最终应用租约结束时清理快照；异常残留只按已记录的目录身份清理。

宿主需要 Python 3 的 GIO 绑定。原有 `xdg-dbus-proxy` 继续限制可访问的服务；独立 `files-bus` 接入原生工具包，原 `bus` 保留给已有命令助手和其他连接。记录只包含文件交付元数据和首次读取事件，位于管理根 `.lab/portal-delivery/`，不包含文件正文。读取事件不能独立证明远端上传完成，必须由用户核对上传结果。

## 停用与缓存

```sh
unifier app file-chooser <应用ID> native
```

完整退出后重新启动，Unifier 不再包装命令或注入 GTK/动态库设置。用户原先自己配置的环境保持原样。

私有缓存按规则、规范库路径、设备/inode、大小和修改时间生成身份摘要，复用相同条目，不为每次启动不断创建目录。它只含库链接，不保存聊天、所选文件或账号资料。缓存保留以免破坏其他仍在运行的应用；停用后不再进入搜索路径。系统清理临时目录或确认无相关运行进程后清理对应客体用户的缓存即可，无需改宿主环境。

本功能首先以离线合同和合成共享库验证，不把测试通过等同于已部署或钉钉真实工作流通过。

本轮故障链、提交边界、验收证据和未完成设计见 [Portal 文件交付：设计与验收](Portal文件交付-设计与验收.md)。
