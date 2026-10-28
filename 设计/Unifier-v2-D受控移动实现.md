> 后续状态：D 已通过主控本机真实验收并存独立恢复点，详见 `evidence/unifier-v2-stage-d-complete-2026-10-25/report.md`。进入 E 后同一最终源码的 C/D 组合复验也通过；下文早期主控 gate pending 属于实现时点。

# Unifier v2 D：受控移动（lease + 私有隔离区）实现说明

日期：2026-10-25，Asia/Shanghai。分支：`codex/unifier-v2-control`。
状态：**实现与本地独立探针已通过，等待主控 D 复核/验收；本文件不标记 “accepted”。**
范围仅限普通文件的受控 move 源删除；E 尚未开始。

## 1. 公共入口

```python
control = Control(project_root())

# 只有显式 cleanup_policy="controlled" 的 read-write 本地源根才可能成为删除源
source = control.add_root(
    "/abs/source", access_policy="read-write", cleanup_policy="controlled")
target = control.add_root("/abs/target", access_policy="read-write")

# 默认 cleanup_source=False：只复制/发布/登记，源永久保留
record = control.move_file(
    source["root_uuid"], "relative/source.bin",
    target["root_uuid"], "relative/published.bin")

# 显式逐操作授权：只有授权、受控读写根、目标与登记在最终边界仍有效时才删除
record = control.move_file(
    source["root_uuid"], "relative/source.bin",
    target["root_uuid"], "relative/published.bin",
    cleanup_source=True, fault_hook=None)
```

对应 CLI：

```bash
python3 tools/unifier.py root add /abs/source \
  --policy read-write --cleanup-policy controlled
python3 tools/unifier.py root add /abs/target --policy read-write

# 默认保留源；该命令停在 SOURCE_CLEANUP_PENDING，exit 1
python3 tools/unifier.py file move SOURCE_ROOT REL TARGET_ROOT REL

# 显式授权后才进入受控删除协议
python3 tools/unifier.py file move SOURCE_ROOT REL TARGET_ROOT REL \
  --cleanup-source

python3 tools/unifier.py file status [OPERATION_UUID]
python3 tools/unifier.py file list
python3 tools/unifier.py file recover [OPERATION_UUID]
```

`--cleanup-source` 只对 move 有效；copy 永远保留源。旧登记记录没有
`cleanup_policy` 字段时按 `retain` 读取，绝不由后来的策略变更获得删除权。
`fault_hook` 仅测试使用，不暴露给正常 CLI。

## 2. 删除授权与前置拒绝

每个受控操作在 journal 中固定写入 `source_cleanup` 快照：

- `requested=true`、`authorized=true`、`mode="controlled"`；
- `policy_snapshot="controlled"`、源根 UUID/dev/ino/locator、access policy、
  backend、source-view 标志；
- 隔离区计划、捕获 inode、lease 与最终删除意图。

缺块、旧 schema 块、`authorized` 非 true 或快照不是 controlled 的 journal
都不能在恢复时获得删除权。删除前后都重新读取 registry，而不是复用缓存对象。

前置拒绝发生在任何复制或 journal 副作用之前：

- 源根未登记 `cleanup_policy=controlled`；
- 源根不是 `read-write`；
- 源根是 C 只读视图或非 `local-directory` backend；
- 源/目标不是由已登记根 UUID 选择，或违反嵌套根规则。

最终删除前再次核对所有条件：当前根登记仍为 controlled/read-write、根
dev/ino/locator 与快照一致、源父目录路径仍指向持有 FD、目标仍是已发布
inode、登记仍恰好一条且匹配、目标内容哈希/大小/链接数仍正确。

## 3. 持久检查点

独立探针观察到的检查点（贯穿复制、发布、登记和受控清理）：

```text
INTENT_DURABLE
STAGING
STAGING_CREATED
STAGING_READY
TARGET_PUBLISHED
REGISTRY_COMMITTED
STAGE_CLEANED
SOURCE_CLEANUP_INTENT
SOURCE_QUARANTINE_INTENT
SOURCE_QUARANTINE_READY
SOURCE_CAPTURE_INTENT
SOURCE_CAPTURED
SOURCE_ISOLATED
SOURCE_DELETION_INTENT
SOURCE_REMOVED
DONE
```

```text
INTENT_DURABLE -> STAGING -> TARGET_PUBLISHED -> REGISTRY_COMMITTED
  -> SOURCE_CLEANUP_INTENT
  -> SOURCE_QUARANTINE_INTENT -> SOURCE_QUARANTINE_READY
  -> SOURCE_CAPTURE_INTENT -> SOURCE_CAPTURED -> SOURCE_ISOLATED
  -> SOURCE_DELETION_INTENT -> SOURCE_REMOVED -> DONE
```

`SOURCE_CLEANUP_PENDING`、`CONFLICT`、`NEEDS_RECONCILIATION` 是保守终态：
保留可解释的字节并等待人工核对；不会为了“完成”而猜测删除。
恢复从 journal 状态继续，终态恢复是 no-op，可重复调用。

## 4. lease 与私有隔离区

`src/multilinux/lease.py` 只用真实内核 `F_SETLEASE`/`F_WRLCK`：

- 块通知使用一个专用实时信号，`F_SETOWN_EX/F_OWNER_TID` 指向调用线程；
  该线程在 lease 生命周期内阻塞此信号，并用 `sigtimedwait` 排空；
  不安装进程级 SIGIO handler，不全局忽略 SIGIO。
- 受控清理只允许在主线程执行；其他线程上的请求保守保留源，因为本模块
  不修改其他线程的信号掩码。
- lease 是“已打开 inode 的内容写入屏障”，**不是 rename/unlink 的命名空间
  CAS**。因此捕获前仍要验证源名字、dev/ino、哈希；删除前仍要重新验证
  目标路径、登记与隔离区 inode。
- 捕获使用 `renameat2(RENAME_NOREPLACE)`，把源名字移入源根同一文件系统内
  的随机 `.ml-quarantine-<operation>-<nonce>` 私有目录（0700，创建后 fsync）。
  已有同名目录但没有已持久化 dev/ino 时，绝不 chmod、收养或删除。
- 捕获后校验隔离条目的真实 dev/ino/大小/哈希，并复用已持有 lease 的同一个
  open file description；不会再次 open 已被 lease 的 inode 而造成自断 lease。
- 删除前对捕获条目执行最后一次 `nlink == 1`、哈希、隔离目录路径 dev/ino、
  原源父目录路径、目标 inode/路径/内容/链接数和 registry 校验，并确认源与
  目标 lease 仍为 held。只有全部通过才 unlink 隔离区的 `source`。
- 原源名被新的无关对象重新占用时，保留新对象；捕获的原始字节仍在隔离区
  时按需 `NEEDS_RECONCILIATION`，不会覆盖原路径。
- 捕获到竞态外来 inode 时，能用 `RENAME_NOREPLACE` 安全还原则归还；不能
  安全归还则继续留在私有隔离区并进入 `NEEDS_RECONCILIATION`。

## 5. 恢复语义

- 隔离区已 pin dev/ino：只按 pinned inode 继续；路径被替换即拒绝。
- 隔离条目已捕获：重新打开一次、加 lease、按 journal 的源版本与哈希验证，
  再继续后续边界；不会因恢复而把 `SOURCE_CAPTURED/ISOLATED/DELETION_INTENT`
  倒退成 `SOURCE_QUARANTINE_READY`。
- `SOURCE_DELETION_INTENT` 且隔离条目缺失、隔离目录为空：视为已完成但未写入
  `SOURCE_REMOVED` 的私有 unlink，补写 `SOURCE_REMOVED` 后完成；目录非空则
  拒绝推断删除。
- `SOURCE_REMOVED` 后重复恢复只清理已 pin 的空隔离目录并保持 `DONE`。
- 任一未知窗口、外部抢占、策略撤销、根/目标替换、链接数变化、lease 中断：
  保留字节，写 `SOURCE_CLEANUP_PENDING` 或 `NEEDS_RECONCILIATION`。

## 6. 本轮修复

主控复核指出的最终边界问题已落实到产品代码：

- 捕获后的 lease 复用同一个 open file description，避免新 open 自断 lease；
- 最终删除边界重新加载 registry/根/父路径/目标路径并重新计算目标哈希；
- 恢复不重置已持久化的后续清理进度，未知隔离目录身份不收养；
- **新增最终捕获源 inode 的 `nlink == 1` 与哈希复核**：在
  `SOURCE_DELETION_INTENT` 之后若有人从隔离区条目创建硬链接，删除会被拒绝，
  原字节保留，操作进入 `NEEDS_RECONCILIATION`。

## 7. 测试与证据

本轮新增 `tests/test_controlled_move.py`，11 项使用真实临时文件/子进程：

- 正常受控 move：源被删除，目标内容正确，隔离目录消失，检查点完整；
- 受控源根但未显式 `cleanup_source=True`：默认保留；
- 缺少 `source_cleanup` 的旧 journal：恢复后仍保留，永不获得删除权；
- 只读根注册 controlled 被拒绝，未授权 move 在副作用前失败；
- 持有 reader/writer/mmap 时 lease 拒绝捕获，源保留；
- 真实 lease break 被 `observe()`/`assert_held()` 识别；
- 最终目标被替换/改名：拒绝删除，恢复两次仍保留源字节；
- 最终源出现额外硬链接：拒绝删除（本轮回归）；
- 晚到策略撤销：保留隔离区中的原始字节；
- 从 `SOURCE_CAPTURED` 恢复可完成且幂等；
- 隔离目录 dev/ino 被同名新目录替换时拒绝收养，源保留。

执行结果（私有日志）：

- `python3 -m unittest -v tests.test_controlled_move`：11 项全部通过；
- `python3 -m unittest discover -s tests -v`：**304 项，OK（4 项条件跳过）**；
- 独立 native 探针：`dsh-native-attempt1.json` 失败于最终源硬链接窗口（已保留），
  修复后 `dsh-native-attempt2.json` **35/35 cases 通过**，含真实 SIGKILL、
  两次恢复、late policy/root/target 变更、硬链接、lease 与外来捕获；
- legacy 探针 `dsh-legacy-attempt1.json` 通过（20 cases）；
- public CLI 探针 `dsh-cli-attempt1.json` 通过。

日志：`.lab/agents/unifier-v2-stage-d2-20261025/test-controlled-move.log`、
`.lab/agents/unifier-v2-stage-d2-20261025/full-unittest.log`。

## 8. 明确限制

- 不声称 namespace CAS；lease 不阻止其他进程 rename/unlink 源名字。
- 不声称跨文件系统原子 move；仅同一源文件系统内的捕获/隔离，目标侧仍是
  复制 + 同目标文件系统 stage/link 发布。
- 不声称断电持久性证明；只做 fsync 与 journal 顺序的可恢复实现。
- 不防御恶意同 UID 篡改私有 journal/隔离目录；这是同宿主信任边界内的
  fail-closed 协议，不是特权隔离。
- 目录递归、符号链接、硬链接源、设备/FIFO/socket、ACL/xattr/SELinux 标签、
  稀疏文件、完整元数据保真仍不支持或明确拒绝。
- 需要 Linux `F_SETLEASE` 与 `renameat2(RENAME_NOREPLACE)`；不可用时保留源。
- 受控清理只能在主线程执行。
- 最终验收仍以主控 D gate 为准；本文件不替代 `acceptance-result.json`，也不
  进入 E。
