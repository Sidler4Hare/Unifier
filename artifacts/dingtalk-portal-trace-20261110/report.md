# 钉钉上传转发核验

本轮只读核对了日常实例，并在受保护的私有总线复验了转发。钉钉的冷启动和宿主文件选择器的界面验收为 `BLOCKED`。

日常 Debian 实例 `b9dd15c5fccf9f02a1033d6c824066563713fb3f8cfe5bea37b73378417cefdb` 的 init 为 9962。实际 cgroup 及祖先没有测试所需的内存、swap 或 CPU 硬上限，任务上限是 2048，观察时容器有 222 个任务。受保护的固定 Debian `e58754f0645ae1ff2924cb09a7264cc821e44893e7589a7fd0ebca09ce5f5e45` 已停止，登记包清单没有钉钉，客体任务上限 192。现有共享压力回执只放行其 CLI 合同，GUI 准入仍未通过。日常实例及应用配置保持现场状态。

钉钉进程仍是 20:38 启动的 PID 10175，窗口角色为 `GtkFileChooserDialog`。宿主 Portal 服务从 19:00 就已运行，当前通过该客体已有代理执行 `FileChooser.Properties.GetAll` 得到版本 4。日常客体具有 Python 3、PyGObject 和 Gio 类型库，应用 PATH 上没有 KDialog。共享目录宿主与客体的设备号和 inode 分别都是 55 和 2584287。现有启动日志没有记录本次 CEF 的 Portal 请求或厂商回调的运行结果。

私有探针调用正式 `start_bridge`，用真正的 `xdg-dbus-proxy` 连接隔离总线上的模拟 Portal。其顺序是 `NameHasOwner=false`、服务登记、`NameHasOwner=true`、`GetAll` 读取版本 4、执行工作树发布的 KDialog 助手、`OpenFile`、收到 `Response`、核验返回路径与合成原文件是同一 inode 并读取字节。这个探针验证了协议和路径交接，无法说明钉钉实际选择了哪个分支。

首轮探针命令退出 0，保护器因 transient 单元在 show 与 stop 之间消失将总回执记为 `INVALID`。单元、实际 cgroup 和临时路径均独立核对已消失。保护器现只在 stop 返回 5 后再次证实名字未复用、单元已卸载且原 cgroup 不存在时认可该竞态。单元复用或 cgroup 残留继续判为失败。前两份回执保留原样。

最终受保护复验 `acceptance-fixed/` 为 `PASS`，确认独立 cgroup 和临时目录清理成功。中间复验的探针导入路径顺序错误，已在最终复验脚本纠正。新回执同时覆盖 transient 单元正常卸载以及名字复用、原 cgroup 仍存在时的拒绝。没有对日常版部署转发改动，也没有重启钉钉或客体。

## 实机验收需要的证据

1. 准入一台装有相同版本钉钉的受保护实例，登记完整 CID、独立显示、私有 Portal、全树限额和清理边界。只准备一个无账号内容的共享样本。
2. 在启动钉钉前开始采样，记下进程身份、Portal 名称和版本探测、`FileChooser.OpenFile`、KDialog 进程以及对应的窗口角色。只落盘调用元数据和合成样本路径。
3. 若实际分支走 KDialog，启用工作树的 `kdialog-portal` 并复跑。若有 Portal 探测却从未发出选择请求，继续定位 CEF 初始化或厂商回调。若收到 URI 后打开失败，按实际挂载和返回路径修复映射。
4. 在宿主 KDE 选择器选中合成原件，核对返回路径在客体中对应同一文件且钉钉实际读取该文件。点击发送或上传到真实会话不属于判据。
