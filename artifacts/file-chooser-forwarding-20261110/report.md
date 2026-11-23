# Unifier 应用文件选择转发

代码在 `codex/auto-host-share` 工作树。应用可以用 `unifier app file-chooser <id>` 查询模式，用 `unifier app file-chooser <id> <mode>` 修改模式。设置本身不启动容器。现有记录默认采用 `native`，设回它可恢复默认行为。变更会更新包含启动环境的应用执行指纹，已绑定旧指纹的集成需要按现有流程重新登记。

| 模式 | 生效条件和行为 |
| --- | --- |
| `native` | 沿用应用自己的行为。 |
| `gtk-portal` | 应用使用支持 Portal 的 GTK 选择器；启用 GTK 的 Portal 环境变量。 |
| `qt-portal` | 应用使用支持平台主题的 Qt 选择器，客体有兼容的 `xdgdesktopportal` 插件；选择对应平台主题。 |
| `kdialog-portal` | 适用于实际调用 `kdialog` 的应用，客体需要 Python 3 和 PyGObject/Gio。辅助程序把单选、多选、目录选择和另存请求转给宿主 Portal，返回实际挂载内的客体路径。 |

转发模式要求已有按需启动和桌面 Portal 桥。GTK/Qt 的原生 Portal 调用继续沿用现有代理，Unifier 校验至少一个实际同路径共享，代理本身不重写响应。KDialog 辅助程序只接受可映射到既有数据绑定的本地 URI，多选先核验全部结果，另存需要可写绑定。文件读写由客体应用使用映射后的路径执行。动态宿主共享与钉钉的实际选择器接管仍需分别验收。

首轮 Qwen 和回归均在宿主压力准入阶段停止，未启动工作负载。随后等到连续 30 秒低压力并重新准入，67 项定向回归和私有 D-Bus 探针通过。探针从实际发布的辅助程序发出请求，覆盖快速响应、中文路径、另存、取消和拒绝未映射路径。Chromium 的 KDialog 版本判断显示 19.12 是通配符格式的门槛，最终改动同时支持 MIME 和通配符过滤，10 项定向用例及私有 D-Bus 探针复验通过。两个成功批次均已移除临时目录和工作 cgroup。回执保存在 `acceptance/` 与 `filter-acceptance/`。
