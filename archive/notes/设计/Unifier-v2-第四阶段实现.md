# Unifier v2 第四阶段 D：受管普通文件复制与保守恢复（本机受限闭环真实验收通过）

日期：2026-10-24，Asia/Shanghai。分支：`codex/unifier-v2-control`。状态：**D 的受限普通文件复制与保守恢复闭环已通过本机真实验收；293 项回归（289 通过、4 条件跳过），20 项跨文件系统/阶段退出用例、真实 ENOSPC、三发行版 C→D 复制及公共 CLI 通过。自动源删除、原子移动、断电与跨宿主验收未完成。以 evidence/unifier-v2-stage-d-2026-10-24/report.md 为准。**

本轮只实现一个已登记数据根中的普通文件到另一个已登记数据根的复制，以及“复制成功但源保留”的受限移动。目录递归、ACL/稀疏/设备等能力明确拒绝或列为未支持；不声称完整文件元数据保真，不声称原子跨文件系统移动，也不删除源。

## 1. CLI 与 API

```bash
# 普通文件复制；源根可为 read-only/read-write，目标根必须 read-write
python3 tools/unifier.py file copy SOURCE_ROOT_UUID relative/source.txt \
  TARGET_ROOT_UUID relative/target.txt

# 受限移动：目标发布并登记后停在 SOURCE_CLEANUP_PENDING，源保留
python3 tools/unifier.py file move SOURCE_ROOT_UUID relative/source.txt \
  TARGET_ROOT_UUID relative/target.txt

python3 tools/unifier.py file status [OPERATION_UUID]
python3 tools/unifier.py file list
python3 tools/unifier.py file recover [OPERATION_UUID]
```

`file copy/move` 仅在返回记录 `complete=true` 时 exit 0；未完成或冲突返回 exit 1 并保留 JSON 状态。当前受限 move 的 SOURCE_CLEANUP_PENDING 因源尚保留而 exit 1。

`file recover` 不带 UUID 时扫描所有仍可恢复的操作；带 UUID 时只处理该操作。`file status/list` 输出操作 UUID、状态、真实源/目标/暂存路径、源根/目标根 UUID、管理域、provenance、发布 inode、源保留原因和能力边界。

Python API：

```python
control = Control(project_root())
record = control.copy_file(source_root_uuid, "a.txt", target_root_uuid, "b.txt")
record = control.move_file(source_root_uuid, "a.txt", target_root_uuid, "b.txt")
control.file_operation(operation_uuid)
control.list_file_operations()
control.recover_file_operations(operation_uuid=None)
```

`copy_file`/`move_file` 还接受仅供测试的 `fault_hook=` 关键字；正常 CLI 不暴露 `os._exit` 或故障注入入口。测试用子进程在该 callback 到达指定持久阶段时退出，再由恢复路径核对。

## 2. 身份、路径与打开规则

- 源和目标都必须由 `DataRootRegistry` 的 `root_uuid` 选择；不接受裸宿主路径。
- 每次操作重新读取根记录，核对 `root_dev`/`root_ino`；根路径被替换时 fail-closed。
- 从根 FD 逐段打开父目录，使用 `O_NOFOLLOW`，拒绝中间 symlink、跨挂载边界和跨设备边界；最终源文件以 `O_RDONLY|O_NOFOLLOW` 打开并核对 FD 的 dev/ino/mode/nlink。
- 重新检查最长嵌套根规则：通过父根显式进入更具体的已登记子根会被拒绝，必须显式使用子根 UUID 和其策略。
- 目标根策略必须为 `read-write`；只读目标在任何日志或内容副作用之前拒绝。源根允许 `read-only` 或 `read-write`。
- 源为硬链接、目录、符号链接、设备/特殊文件、带 POSIX ACL、稀疏或压缩迹象时拒绝，并在记录中列出未支持能力。
- 发布前后再次核对目标根路径、目标父目录 FD 的 dev/ino 和目标路径；不把“resolve 后的字符串”当作打开凭据。
- 新对象使用**目标根**管理域；源根只是独立 provenance。不复制源 xattr、SELinux 标签、ACL、执行授权、handler 或项目绑定。

## 3. 持久状态机

```text
INTENT_DURABLE → STAGING → TARGET_PUBLISHED → REGISTRY_COMMITTED
              → DONE（copy）
              → SOURCE_CLEANUP_PENDING（move）
                    ↘ CONFLICT / NEEDS_RECONCILIATION
```

- 每个操作有独立 journal：`.lab/state/file-operations/operations/<operation_uuid>.json`，临时文件 fsync 后原子替换，并 fsync 目录。
- 全局操作登记在 `.lab/state/file-operations-registry/registry.json`，只在 `REGISTRY_COMMITTED` 前短事务持有全局锁；长复制只持操作自己的 flock。
- `INTENT_DURABLE` 先持久化源观察版本、根身份、目标计划和覆盖政策；`STAGING` 先记录计划，再在目标父目录用 `O_EXCL|O_NOFOLLOW` 创建唯一 stage，并持久化 stage dev/ino/nonce。
- 复制流核对源 FD 版本；写入后 fsync stage、fsync 目标目录，再读回 stage 计算 SHA-256。复制后重新核对源 FD 身份、大小与哈希，并与 stage 哈希/大小比较。
- 发布使用同一目标文件系统内的 `linkat`（`os.link`，不覆盖）把 stage 链接为最终目标；随后 fsync 目标文件和所在目录。这里不把 `rename` 或“哈希相同”当成 CAS。
- 文件系统发布与 SQLite/JSON 登记不是跨系统原子事务。journal 明确记录两者分离，恢复只按操作 UUID 和 inode 身份核对，不靠哈希认领。
- 注册成功后才清理“目标与 stage 确为同一 inode 且身份吻合”的内部 stage，使最终目标 `st_nlink=1`，可被已有 `FileControl.resolve_file` 和 B 文件计划正常观察。未知同名对象不删除。
- copy 终态为 `DONE`；move 终态停在 `SOURCE_CLEANUP_PENDING`，`source_cleanup.performed=false`、`source_retained=true`，明确“目标已复制、源保留”。

## 4. 恢复与冲突

- 恢复按 operation UUID 加独立 flock，幂等核对 journal；并发恢复不会重复登记。
- `INTENT_DURABLE`/`STAGING`：重新核对根身份、嵌套根、目标父目录、源版本；目标不存在且源未变时可重建或继续 stage。只有 journal 已记录 inode 的 stage 才允许在目标不存在时清理重试。
- 发布前目标已存在：无论内容是否相同都进入 `CONFLICT`，保留目标和源，不覆盖。
- stage 路径被替换：不认领、不 unlink，进入 `CONFLICT`/`NEEDS_RECONCILIATION`。
- 源在暂存后变化：`CONFLICT`，保留源、stage 和可解释路径，不发布。
- `TARGET_PUBLISHED`/`REGISTRY_COMMITTED`：要求目标与 stage 是同一 inode，或（登记后）目标仍匹配预先记录的 published dev/ino；核对目标哈希/大小后才登记或进入终态。
- 目标被同内容无关对象替换、目标 inode 变化、目标内容被原地篡改、根/父目录被替换、媒体缺失：不凭哈希认领，保留字节并进入 `CONFLICT`/`NEEDS_RECONCILIATION`。
- 终态恢复是 no-op；DONE 后目标被替换也不会被重新认领或重复登记。
- `CONFLICT`/`NEEDS_RECONCILIATION` 是保守终态，留给人工核对；不会自动删除未知对象，也不会编造源删除成功。

## 5. C 只读 FUSE 源整合

C 的 ready 只读视图可以显式登记为源根：

```bash
python3 tools/unifier.py root add VIEW_MOUNT --source-view VIEW_UUID
```

登记记录 view UUID、`environment_binding` 和 mountpoint，强制 `read-only` 策略。未实际挂载的空目录会被拒绝，不当作数据根。复制和恢复在需要源时打开并持有源根 FD 和源文件 FD 直到复制完成；因此 C 撤销遇到 busy 时仍应保留 worker/broker/storage 引用。该整合仍只支持普通文件；递归目录、ACL、稀疏、设备和完整元数据能力继续明确拒绝。

## 6. 能力边界

已实现：普通文件复制、受限 move 的目标复制与 source-retained 终态、无覆盖发布、每阶段 journal、按操作 UUID 的恢复、目标管理域登记与源 provenance、C 只读视图源根登记（普通文件范围）。

未实现/不宣称：自动源删除、完整原子移动、跨文件系统原子 rename、目录递归、ACL/稀疏/设备/符号链接/硬链接源、完整 xattr/SELinux/ownership/timestamp 保真、断电持久性证明、跨系统事务、CAS、并发外部写入者的最终 unlink 竞争解决、性能预算。

## 7. 离线回归

`tests/test_file_operations.py` 使用测试 callback 让子进程在 `INTENT_DURABLE`、`STAGING`、`STAGING_CREATED`、`STAGING_READY`、`TARGET_PUBLISHED`、`REGISTRY_COMMITTED`、`STAGE_CLEANED` 退出，并重复恢复两次。另覆盖同内容无关目标、stage 替换、发布后目标替换、原地篡改、源变化、根替换/策略变化、只读目标零副作用、未知 schema、跨文件系统 stage、并发恢复、move 源保留，以及完成 copy 后 `Control.resolve_file` 可观察。完整回归以主控实际执行的 `python3 -m unittest discover -s tests -v` 为准。

主控真实验收与可重跑脚本见 `evidence/unifier-v2-stage-d-2026-10-24/report.md`；本文件中早期“待实测”说明属于实现阶段，当前以该报告和 acceptance-result.json 为准。
