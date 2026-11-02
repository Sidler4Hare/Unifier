# 架构复核与文件归属/恢复机制实验

日期：2026-10-23，Asia/Shanghai。新证据独立于此前实验。

- `baseline.json`：接手时 HEAD、工作树状态、生产源码/测试/配置/工具哈希。
- `unittest.txt`：当前工作树回归，156 项中 152 通过、4 跳过。
- `probe.py` / `result.json`：13 项隔离文件机制检查，及两项 catalog 扩展边界观察。
- `verify.py` / `validation.json`：重算历史 28 波结果、核对本轮结果与源码、确认生产文件未变；记录引用材料的当前哈希。
- [复核报告](../../调研/Grok改动复核与架构迭代-2026-10-23.md)。
- [设计 v2](../../设计/文件与执行控制层-v2.md)。

复跑使用新的输出路径：

```bash
python3 evidence/architecture-review-2026-10-23/probe.py --output /tmp/ml-review-new-result.json
python3 evidence/architecture-review-2026-10-23/verify.py /tmp/ml-review-new-validation.json
```

`verify.py` 核对本目录已保存的 `result.json` 与 `unittest.txt`，不自动读取上面另存的探针结果。输出文件存在时拒绝覆盖。探针使用 Python 标准库和 GNU cp/mv，只写新建临时目录（项目 `.lab`、`/dev/shm`），不启动容器、不安装包、不更改宿主配置。

恢复原型不是产品实现：子进程正常执行到指定断点后用 `os._exit(77)` 模拟进程中断；未测断电或内核崩溃。跨文件系统样本目标为 tmpfs，不证明掉电持久性。注册后仍保留源文件，状态为 `source-cleanup-pending`，未实现最终源删除。未测实际 inotify 队列溢出、并发路径交换、inode 重用、SELinux、ACL、ENOSPC、整个目录树。两项 catalog 观察来自合成配置/记录，没有改写真实环境。
