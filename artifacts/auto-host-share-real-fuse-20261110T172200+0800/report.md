# 宿主目录共享：真实 FUSE 挂载实测

2026-11-10，`codex/auto-host-share` 工作树，基线 `c947077f`。本轮 T2 宿主探针退出码为 0，保护器判定 `PASS`。实际源码和探针指纹见 [preflight.txt](preflight.txt)。

探针调用工作树的 `HostShareViewBackend`、`FuseServer` 和 `mount_with_fusermount`。内核记录了独立挂载命名空间里的 `fuse.unifier` 挂载，标志为 `ro,nosuid,nodev,noexec`。用户命名空间里的 UID 0 只映射到宿主普通用户 UID 1000。探针仅访问本轮私有临时目录。

| 实际文件操作 | 结果 |
| --- | --- |
| 列出目录并直接查找黑名单、管理员目录和符号链接 | 列表只显示公开目录；被过滤的入口直接查找也返回 `ENOENT` |
| 打开中文文件名；宿主覆盖同一文件并创建新文件 | 已打开句柄读到宿主写入的新内容；重新列目录能看到新文件 |
| 从挂载内打开文件准备写入 | 返回 `EROFS`；目前共享只读 |
| 宿主把已打开文件所在目录移进黑名单，再在原路径创建替代文件 | 旧句柄返回 `ESTALE`；按原路径重新打开读到替代文件内容 |
| 正常卸载 | 挂载从命名空间消失，服务线程退出；保护器回执确认工作 cgroup 和临时目录已删除，宿主无该挂载 |

复现命令，从主仓库运行并换用新的 `--out` 路径：

```bash
python3 tools/offline_test_guard.py run-host-fuse \
  --out artifacts/<本轮新回执目录> \
  --probe artifacts/auto-host-share-real-fuse-20261110T172200+0800/probe.py
```

原始证据：[内核探针日志](guarded/command.log)、[保护器回执](guarded/receipt.json)。本轮没有启动容器或 Dolphin，也没有部署。常规 `run_fuse` 入口仍创建现有 `ViewBackend`，缺少新后端到客体可见挂载路径的接线。需要完成接线并通过实机准入后，才能验证原版 Dolphin 的打开、复制和位置入口。客体写回未实现。
