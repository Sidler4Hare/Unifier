# 文件层机制与成本实验

2026-10-23。报告：[可行性与性能评估](../../调研/文件层可行性与性能评估-2026-10-23.md)。

- `probe.py`：本机原生、私有 namespace bind、本地 SFTP + FUSE 两种缓存配置的对照。复跑需新的 `--output` 文件。
- `result.json`：全部 28 波正式结果、4 次预热、7 波 SQLite 查询、环境记录与清理。
- `summary.json`：中位数、范围、比值和已取得的内存记录。
- `validation.json`：输出哈希、样本数量、保存与清理核验。
- `pilot.json`：先行验证本地 socketpair 文件服务可挂载和读取；挂载已清理。
- `base-images.json`：当前锁定基础镜像的 Podman `.Size`，不是完整安装或独占磁盘占用。

复跑示例：

```bash
python3 evidence/file-layer-feasibility-2026-10-23/probe.py --output /新的绝对路径/result.json
```

依赖当前已安装的 Python、sshfs、OpenSSH sftp-server、fusermount3、unshare/mount；不安装宿主包、不联网、不使用已有容器。只在项目 `.lab` 下创建本轮临时数据和挂载，正常结束清理。输出存在时拒绝覆盖。

本轮数据源为 2,000 × 4 KiB 和 128 MiB 大文件。所有访问结果校验通过。源页缓存已预热，顺序读取包含 SHA-256；保存包含文件 fsync，但没有目录 fsync。命令启动和挂载创建不计入 workload 时间。bind 对照不是完整容器。

SFTP server 的 PSS 读取被拒绝，错误保留，不当作零内存。只测到的 SSHFS 客户端 PSS 不能作为整条服务总占用。此实验不代表优化后的自有 FUSE、真实容器文件服务或产品兼容性验收。
