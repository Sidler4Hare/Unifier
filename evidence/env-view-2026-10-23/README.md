# 停止容器的环境视图

2026-10-23。报告：[环境文件视图原型](../../调研/环境文件视图原型-2026-10-23.md)。

- `probe.py` 与 `result.json`：宿主直接 `podman mount`。rootless 下命令被拒绝。
- `probe.py` 与 `result-unshare.json`：同一脚本的后续版本改为 `podman unshare podman mount`，并核对链接、卷目录、upper 和 uid。`host_can_list_stopped_root: true` 时名单仍是空数组，表示宿主只打开了空的 merged 目录。
- `probe_save.py` 与 `result-save.json`：在存储命名空间里写入已停止的合并根，重启后由容器 root 读回。

复跑会新建临时容器和卷，并在结束时删除。输出文件已存在时拒绝覆盖。不 pull、不装包、不操作脚本启动前已经存在的容器。

```bash
python3 evidence/env-view-2026-10-23/probe.py --output /新的绝对路径/result.json
python3 evidence/env-view-2026-10-23/probe_save.py
```

`probe_save.py` 固定写本目录的 `result-save.json`。要重跑先移走该文件。临时容器带 `label=disable`，避免实验改写标签；这不是产品默认。
