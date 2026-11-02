# C 阶段真实验收

C 已在本机通过实际验证。由 dsh Command Code deepseek/deepseek-v4.1-flash 实现，主控独立执行容器、FUSE、GUI 和清理验收。最终完整回归 **270 项，266 通过、4 项条件跳过**；以 `final2-gate.json` 及其中引用的日志和 JSON 为准。前一个 final-gate-run.json 之后还有一次 peer 检查修改，因此 final2 针对不再变化的最终源码重跑。

| 验收 | 结果 |
|---|---|
| Debian 13 / Fedora 44 / Arch | 停止状态下真实宿主 FUSE；合并根、独立 named volume、嵌套 bind 可读；StartedAt 不变 |
| 原始与投影链接 | cat 跟随绝对、相对、向上链接读取客体；raw 元数据保留原文；宿主逃逸哨兵不可读 |
| UID/GID | 默认 rootless 和 Arch keep-id 的 guest 0/1/1000/1001 正确呈现；不 chown/relabel |
| 只读 | 写、创建、删除、改名、chmod、mkdir、symlink、hardlink 均 EROFS；活动 view 拒绝 start/remove/重复 open |
| 后端丢失 | 卷缺失时 open 拒绝；活动卷消失或被空目录替换时报 EIO，无假空目录 |
| 生命周期 | 持 FD 时撤销被拒绝且两服务、底层引用保留，仍可读；worker/broker SIGKILL 后恢复两次；外部 storage ref 保留 |
| 原生图形应用 | Dolphin 显示客体根；KWrite 经投影链接显示 Debian 13 os-release；环境始终停止，窗口已关闭 |
| 清理 | view registry、宿主 FUSE、Podman storage mounts 均空，无本轮 broker 残留 |

原有 26 个容器 ID/状态、主项目 23 个状态文件、12,670 个历史证据文件保持原样；B 阶段 SHA256SUMS 全部通过。暂留 3 个本次测试容器供下一阶段复验，另保留 1 个日常 FreeCAD 环境。

FreeCAD 原先只做了测试应用/handler 登记，验收后清理，未导出菜单。现在保留独立日常环境，入口 `/home/omg/.local/share/applications/unifier-freecad.desktop`，项目 `/home/omg/文档/Unifier-CAD`。已两次从桌面 ID 启动真实主窗口、正常关闭并自动停止，详见 `freecad-desktop/README.md`。

范围为本机 rootless Podman overlay 和本次持久挂载/身份组合。FUSE 是管理投影；原始链接由 readlink-raw 提供。宿主 worker 用自身权限读取，没有 namespace CAP_DAC 代读。mounting/releasing 等无法证明完成的窗口保留 needs-reconciliation，不宣称任意时点或宿主断电自动恢复；未做压力、跨宿主或性能验收。

初轮 AF_UNIX 长路径、SCM_RIGHTS、PID 包装层和引用泄漏已修复，失败历史保留。旧 dsh 大会话请求 TIMEOUT 后用同套餐新会话续修，最终 fresh-lifecycle 调用 exit 0；主控验收针对其最终源码。C 通过后进入 D 的受管复制与恢复，源清理仍须单独证明。
