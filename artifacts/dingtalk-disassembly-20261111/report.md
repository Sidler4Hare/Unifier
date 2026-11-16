# 钉钉选择普通 GTK 对话框的二进制定位

2026-11-11。范围：当前运行的钉钉 8.3.1-Release.261008001；定向 ELF 反汇编、已有冷启动证据、同一 PID/start 的少量只读状态。不附加调试器、不注入、不修改应用或容器、不操作桌面。

## 结论

存在两层独立逻辑，不能再将问题笼统描述成“所有探测都没失败”：

1. **CEF 的 Portal 探测成功，但钉钉业务对话框绕过其标准分派。** 钉钉主库自己的 `FileDialog::doModal()` 选择 `doModalUseGtk()`，不进行 KDE/KDialog/Portal 后端选择。
2. **钉钉自带 GTK 实现的 Native 加载实际失败。** `loadGtk3()` 请求 `libgtk-3.so`；当前环境提供的是 `libgtk-3.so.0`，前者在已核验的搜索位置及 ld.so.cache 中不存在。当前进程已尝试加载，但模块句柄和六个 Native 函数指针全部为零。`gtkNativeAvailable()` 因而返回 false，构造器进入 `gtk_file_chooser_dialog_new()`。

第一层解释 Unifier 的 kdialog 助手为什么收不到请求；第二层解释为什么落到传统 GtkFileChooserDialog 而不是 GtkFileChooserNative。直接失败条件已由运行状态和实际分支核实；缺少无版本库名是对应的环境/加载代码不匹配。没有保存当时的 g_module_error 字符串，也没有动态调用栈，不能把静态调用图称作逐调用运行日志。

## 文件身份

实际 `dingtalk_dll.so`：157480816 字节，SHA256 `dbf0ec801c4d76b7db06fab8fab86ae7c95a80be86f9078ef1fa23fcc4442807`。路径及摘要见 `vendor-fingerprint.json`。

取状态的宿主 PID 1508127，starttime 7466233，读取前后均核验；ELF 基址 `0x7f5c8cbd8000`。下面地址均为 ELF VA，不是跨启动固定的进程地址。objdump 在未命名数据附近给出的相邻 C++ 符号标签不代表变量真实名称；变量语义来自读写指令和加载参数。

## 业务层明确选 GTK

- `FileDialog::doModal`，`0x13937a0`，全函数见 `vendor-modal.asm`。
- 它读取的布尔字段是 `sync`（字符串 VA `0x7500862`），控制同步/回调行为，不是选择 GTK 或 Portal。
- 未被已有对话框回调占用时，最终在 `0x13938fd` 调用 `FileDialog::doModalUseGtk`。本函数没有到 `doModalUseProtable` 的分支；仅仅发现二进制含有后者不能推断会自动选用。
- `doModalUseGtk` 的打开文件路径在 `0x1392f47` 调用 `ShowOpenDialogSync`，后者构造 `GtkFileDialog`。见 `vendor-gtk.asm`、`gtk-show-open.asm`。
- `GtkFileDialog` 在 `0x1396330` 调用 `gtkNativeAvailable`，false 跳至 `0x1396600`，在 `0x1396626` 调用 `gtk_file_chooser_dialog_new`。见 `gtk-ctor.asm`。

另一个可见的 CEF 业务入口 `DGCefView::OnFileDialog` 会绑定 `OnFileDialogContinue` 并在 `0x132ed72` 返回 true；继续函数通过字符串 `FileDialog` 查找业务对话框，然后调用虚表 +0x90。FileDialog 虚表地址点 `0x90e8078` 的 +0x90 项 `0x90e8108` 重定位到 `FileDialog::doModal`。见 `vendor-cef.asm`、`vendor-continue.asm`。这是静态可达链，不宣称本次点击已取得该 CEF 回调的运行栈；聊天按钮也可能直接走同一个业务 FileDialog。

## Native 加载失败的具体机制

`gtk-loader.asm`：

- `gtk3Available()` (`0x138de50`) 在本版本正常返回时固定为 true。
- `loadGtk3()` (`0x138dcf0`) 先检查并置位 `0x9341ba0`，随后才尝试加载；失败后没有清除标志。下一次调用会直接返回，因此当前进程不会自行重试。
- `0x138de20` 的参数字符串在 `0x6965045`，内容为 **libgtk-3.so**；`0x138de2c` 调用 `g_module_open`，返回值存到 `0x9341bd8`。
- 若句柄非零，才依次解析六个函数。`gtkNativeAvailable()` 要求六个指针全部非零；见 `gtk-native-available.asm`。
- 实际映射的 libgmodule 中 `g_module_supported()` 正常返回 true，见 `gmodule-supported.asm`；这里不是宿主不支持动态模块。

读取结果 `native-live-state.json`：

| 状态 | 值 |
| --- | --- |
| loader_attempted | 1 |
| gtk_module_handle | 0 |
| native_new / set_modal / destroy / show / hide / run | 全部 0 |
| 已映射 GTK | libgtk-3.so.0.2417.32 |

`loader-path-evidence.json` 和 `gtk-native-exports.txt`：

- 应用根、plugins/dtwebview、/usr/lib/x86_64-linux-gnu、/lib/x86_64-linux-gnu、/usr/local/lib、/usr/lib、/lib 均无 `libgtk-3.so`。
- 主库 RUNPATH 为 `./plugins/dtwebview/:./`，进程 cwd 是该应用根，无 LD_LIBRARY_PATH。ld.so.cache 仅登记 `libgtk-3.so.0`。
- 主库 DT_NEEDED 本身使用正确的 `libgtk-3.so.0`，所以传统 GTK 能正常运行，而它额外的 Native 加载仍失败。
- 已安装的 GTK 导出了所需全部六个函数：`gtk_file_chooser_native_new`、`gtk_native_dialog_set_modal/destroy/show/hide/run`。不是 GTK 版本缺少 Native API。

## 与此前冷启动结果的关系

`../dingtalk-cold-observation-20261111-r1/report.md` 已观察到 CEF 对 Portal 的 NameHasOwner=true、FileChooser version=4，内部 available/complete=1；首次上传 GTK 窗口出现时标准 chooser 缓存仍为 0。现在找到的是另一套位于 dingtalk_dll.so 的 Native 检测失败，不否定该次 CEF 探测成功。

安装版 r4 的 `src/unifier/file_chooser.py` 只为 kdialog-portal 发布 PATH 助手和 KDE 环境。应用没有执行 kdialog，这一适配不可能接管进程内 GTK 构造函数。gtk-portal 模式会设置 GTK_USE_PORTAL=1；当前进程没有该变量。

## 修复落点与尚未验收项

应先修复这个应用对 GTK Native 的库名解析，使其找到已经存在的 libgtk-3.so.0，再让 Native 路径明确走 Portal。可评估应用私有、可回退的库名兼容方案或上游将加载名修正为 SONAME；不应为一个库名盲目安装整套开发包，也无需重新折腾 KDE/CEF Portal 探测。

仅补库名不代表一定出现宿主 Portal：还要验证 Native 后端选择、设置对应 GTK Portal 环境，并验收 OpenFile、取消、文件路径可达性。当前 Unifier 的 toolkit Portal 模式要求同路径共享，不能直接套用 kdialog 助手的路径转换假设。

本轮没有实施库链接、二进制补丁、环境切换、产品修复或部署；没有新建/启停容器。没有用读取动态库等价替代真实上传工作流验收。

## 收尾

- `guard/` 的 CEF 有界扫描、`vendor-guard/` 的主库反汇编、`native-state-guard/` 的标量读取均原生退出 0，保护 reason=null，工作 cgroup/私有临时目录已收尾；各回执保留。
- Native 状态读取借用统一保护执行器，只读访问既有用户进程；回执模板中的 “T1 offline only” 不代表它是完整离线测试，更不代表日常钉钉被纳入硬预算。授权依据是当前规范第 8 节的人工工作流只读观察。
- 日常钉钉保持用户原状态；本轮只增加本证据目录中的脚本、反汇编、状态和报告，未提交、暂存或改变已安装产品。
