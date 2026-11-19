# WPS Writer 的 KDE 文件选择器

此适配对应 Debian 13 amd64 上的 WPS Office 12.1.2.29183。先核验安装包实际带的 Qt 和文件选择库。版本字符串相同也需要匹配库摘要。

Writer 默认使用 WPS 的文件选择器。打包版的 `wps-portal` 只在客体启动时校验厂商库和适配库摘要，并在该次进程中启用系统对话框。应用配置文件照常由 WPS 管理。厂商 Qt 5.12.12 使用 `kso_qt` 命名空间，主题类和文件对话框类分别增加了 3 个和 1 个虚函数。Qt 补丁按实测虚表构建，并保留宿主 Portal 返回的文件类型。

Writer 的另存为分支把可选格式用两个空格连接，再用 `;;` 拆分原字符串，按字符串全等计算格式索引。失败时返回索引 0，造成 `.docx` 写成旧版 Word 二进制。加载器适配只在该版 WPS 文件选择库的两个调用处识别这种格式列表，将分隔符传给正常的 Qt 拆分函数。应用二进制由厂商包提供。

## 重建

准备 Qt 5.12.12 Linux GCC SDK 的 qtbase 归档、同版 QtBase 源码和已验明版本的 `office6`。构建器核验归档及厂商库的 SHA-256 后离线构建。运行时依赖留在原安装位置。[Qt SDK](https://download.qt.io/online/qtsdkrepository/linux_x64/desktop/qt5_51212/qt.qt5.51212.gcc_64/5.12.12-0-202111241434qtbase-Linux-RHEL_7_4-GCC-Linux-RHEL_7_4-X86_64.7z) · [QtBase 5.12.12 源码](https://download.qt.io/archive/qt/5.12/5.12.12/submodules/qtbase-everywhere-src-5.12.12.tar.xz)

```sh
python3 tools/wps_portal/build.py \
  --qt-sdk /path/to/5.12.12-0-202111241434qtbase-Linux-RHEL_7_4-GCC-Linux-RHEL_7_4-X86_64.7z \
  --qtbase /path/to/qtbase-everywhere-src-5.12.12.tar.xz \
  --wps-office6 /path/to/wps-office/office6 \
  --out /path/to/new-output
```

构建需要 `7z`、`patch`、GCC、G++ 和 binutils。结果保存在 `bundle`，含两个适配库、输入输出摘要和 Qt 许可文件。发行包把该目录放在 `src/unifier/wps_portal_assets`。有管理式 Portal 桥的 Debian 13 将 Writer 菜单登记为 `wps-portal`。其他 WPS 构建继续使用应用自己的文件选择器。已有登记可用 `unifier app file-chooser <应用ID> wps-portal` 切换。

## 验收

在专用实例从应用菜单启动，打开并编辑一个包含正文、表格的 DOCX，经 KDE 分别选 `Word Documents` 与 `Word 97-2003 Documents` 另存。检查 DOCX 包含 `word/document.xml`，正文和表格完整。DOC 要有 `WordDocument` 流。用 Writer 的「打开」经 KDE 重开 DOCX，核对新内容和正常退出。仅看到文件扩展名或 KDE 窗口不足以通过。

初版适配的 GUI 记录和格式验收见[本次回执](../../artifacts/wps-freecad-20261119-dEcC0w5m/report.md)。
