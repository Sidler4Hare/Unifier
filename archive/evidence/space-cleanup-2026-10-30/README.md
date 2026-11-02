# 空间清理（2026-10-30）

清理前项目约 462 MiB，主要来自 evidence（约 318 MiB）、.lab（76 MiB）与 .git（62 MiB）。tests 连缓存约 2.1 MiB，真正的 Python 测试源码仅 707,074 字节（约 690.5 KiB）。测试函数 AST 完全相同的重复项扫描结果为 0；这不证明不存在语义重复，但没有足够依据直接批量删除。

本轮操作：

- 删除 916 个未被 Git 跟踪的 `.pyc` 缓存：逻辑大小 29,444,681 字节、已分配块约 30.05 MiB。只处理清单内缓存，不递归删除其他数据。
- 将 Stage F 三套历史工作负载的 `sequential.bin`（合计 50,331,648 字节，即 48 MiB）保存为同目录 `.xz`（合计 9,472 字节）。仓库内压缩副本与外部恢复副本均逐一解压校验 SHA-256 后，才移除原文件。
- 为这三个可恢复生成文件添加精确范围的忽略规则，后续解压副本不需要再次提交。历史 Git 对象没有删除或重写，因此本轮不会缩小已有 Git 历史。
- 没有删除测试源码、产品代码、legacy worker、日志、截图、状态、验收结论或恢复备份；没有改变日常容器。

原始缓存盘点见 `cache-inventory.json`；压缩数据的 SHA-256、权限和时间记录见 `compressed-fixtures.json`。外部恢复副本在 `/home/omg/文档/Unifier-backups/20261030-space-cleanup/`。清理统计见 `result.json`，实际目录占用前后见 `before-du.txt` / `after-du.txt`。这些是目录分配块变化，不代表 Btrfs 快照/压缩/共享 extent 下物理磁盘空闲空间的精确增加。

## 恢复旧工作负载

普通离线单元测试不依赖这三份历史数据。重跑旧 Stage F 探针或验证旧文件清单前，在仓库根目录执行：

```bash
xz -dk evidence/unifier-v2-stage-f-2026-10-26/implementation/workspaces/{debian13,fedora44,arch}/dataset/sequential.bin.xz
sha256sum -c evidence/space-cleanup-2026-10-30/restored-fixtures.sha256
```

`xz -dk` 保留压缩副本，已存在的输出默认不覆盖。仅恢复数据不等于旧探针可直接重跑：旧容器此前已清理，仍需按原探针准备流程构建新的隔离环境。

普通 Python 使用会重新生成缓存，这是正常行为。本轮验证用 `PYTHONDONTWRITEBYTECODE=1` 避免回填刚删除的缓存。完整回归见 `unittest.log`；没有因为清理而删测试、降低断言或启用真实容器集成。

本轮未提交或推送；Git 中的三项 `.bin` 删除应和对应 `.xz`、校验清单及本说明一起审阅。
