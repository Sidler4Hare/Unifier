# Unifier v2 第三阶段 C：实现与离线回归（待主控真实验收）

日期：2026-10-24，Asia/Shanghai。分支：`codex/unifier-v2-control`。状态：**C 代码/离线测试完成；无真实 Podman/FUSE 验收结论；C 通过前不进入 D。**

本目录保留首轮真实失败尝试 `initial-live-attempt.json`、三个真实 fixture `fixtures.json` 及预置 `live-freecad/`；本轮未修改或删除这些文件，也未创建/启动/停止/删除任何真实容器、卷或 mount。

## 首轮阻断点与修复

首轮真实调用 `ViewManager.open('view-debian13')` 的原始错误是 AF_UNIX 路径超过 107 字节；`_cleanup_failed` 又因 `Directory not empty` 覆盖了原始错误，并遗留 `view_uuid=4facba0d-22e7-459d-89b9-60f94ef12e67`、state=revoking、spec.json。修复：

1. **短私有 socket 目录**：AF_UNIX socket 不再放在长中文项目路径下，优先 `$XDG_RUNTIME_DIR/unifier-views/<view_uuid>`，不可用或过长时 `/tmp/unifier-view-<uid>-<uuid>`；创建时验证 owner、0700、无 symlink。socket 名为 `s`/`c`，长度实测小于 107。
2. **状态先于资源**：在 lifecycle flock 内先写 view record，包含 mountpoint、runtime_dir、control/storage socket、spec/log 的计划位置和 runtime identity；再创建目录/进程。失败时清理只处理本 view 已知文件/空目录，错误写入 `cleanup_errors` 并保留 `failed` 记录，原始异常原样重抛。
3. **legacy revoking 恢复**：`view recover`/`revoke` 使用统一清理，能删除旧记录中 `spec_path=null` 但实际存在的 `spec.json`；busy mount 不 lazy unmount，保留可读的 ready 记录（worker/broker/storage/lease 均保留）。遗留 fixture 可由主控执行：
   ```bash
   python3 tools/unifier.py view recover 4facba0d-22e7-459d-89b9-60f94ef12e67
   ```
4. **SCM_RIGHTS 解析**：`recvmsg` 的 ancillary 现在是 `[(level,type,bytes)]`；代码真实解析 `SCM_RIGHTS`、拒绝截断/多 fd，错误时关闭已收 fd 并终止 broker。真实 socketpair 测试在 `tests/test_views.py::ScmRights`，不 mock ancillary 形状。
5. **UID/GID 映射**：`/etc/subuid|subgid` 按用户名（`pwd.getpwuid`）及数字查找；broker 上报 storage namespace `/proc/self/uid_map|gid_map`，worker 与 Podman `HostConfig.IDMappings` 组合 host→storage→guest。覆盖 guest 0/1/1000/1001；host FUSE 访问仍是 mount 者凭据，不假定 namespace CAP_DAC，guest metadata 单独呈现。
6. **持久 volume/nested bind**：inspect 的 named volume 作为持久数据；显式 `persistent_mounts` 可冻结；嵌套在持久 volume/shared 下的实际 bind 参与最长 destination 覆盖，不再被误标 runtime capability；未知 bind 仍 fail-closed。冻结 source/options 缺失或变化返回 `BACKEND_UNAVAILABLE`。
7. **只读与链接**：FUSE 挂载 `ro,nosuid,nodev,noexec`，无 `allow_other`，mutation 回调 EROFS；absolute/向上 relative symlink 改写/归一到投影内，`readlink_raw` 保留原文并标 projected-link；路径 anchored FD + 逐段 `O_NOFOLLOW`，源/mount 替换 fail-closed。

## 生命周期阻断点修复（待真实复测）

本轮只修复 C 生命周期，未运行任何真实 Podman/FUSE 资源操作：

1. **真实 broker 身份**：broker ready 元数据返回内部 Python 的 host PID/start-stamp/nonce；父进程用 `SO_PEERCRED`、pid+stamp、nonce 和父进程/会话来源核对，保存真实 broker PID，outer `podman unshare` PID 只作 launcher 并可靠 `wait`/回收。
2. **持久 storage journal**：broker 在 `podman mount` 前 fsync `mounting`，成功后 fsync `mounted`，退出前 `releasing`→`released`。正常退出释放自身一次引用；broker SIGKILL 后只有 journal 明确 `mounted` 且 FUSE 已卸载、worker 已结束时，恢复路径才执行一次 `podman unshare podman umount`。`mounting`/`releasing`/`release-failed` 保留 `needs-reconciliation` 并阻断；外部事先 mount 的引用不被多卸。
3. **cleanup 顺序**：先正常 `fusermount3 -u` 宿主 FUSE；busy 时原 worker、broker、storage、socket/log/journal 和 lease 全部保留，视图仍可读且 start/remove 被拒。成功卸载后才结束 worker、broker 并释放 storage；所有资源确认清理前不删除 lease。
4. **真实 readiness**：worker 只有完成内核 `FUSE_INIT`、写出带 view UUID/nonce/pid/stamp 的持久 ready 标记，并能通过 control socket 应答 `status` 后，父进程才标 `ready`；仅看到 mountpoint 不算 ready。
5. **FUSE fd 解析**：保留真实 `SCM_RIGHTS` ancillary 解析，新增真实 socketpair 单 fd/多 fd 拒绝测试，避免把 ancillary tuple 当 fd。

## 验证

本轮全量离线回归：

```text
Ran 270 tests in 4.301s
OK (skipped=4)
```

即 266 通过、4 项既有现场集成测试按条件跳过。完整原始 unittest 日志保存在私有 `.lab/agents/unifier-v2-stage-c-20261024/`，未写入本公开 evidence 目录。C 测试：

- `tests/test_views.py`：真实 socketpair SCM_RIGHTS 单/多 fd、broker `SO_PEERCRED` pid/stamp/nonce 身份、busy FD 保留进程/引用/文件、FUSE 先卸载再停 worker/broker、journal mounted 只释放一次且外部引用保留、mounting/releasing/release-failed 阻断、缺 journal/身份不符阻断、broker/worker SIGKILL 重复 recover、start 接线、view lease 阻塞 start/remove、遗留 revoking spec 恢复。
- `tests/test_view_backend.py`：absolute/relative-up 链接投影、`..`/capability 拒绝、最长 mount 覆盖、缺失源、源替换、file mount、keep-id/default UID 范围组合。
- `tests/test_fuse_view.py`：挂载选项、mutation/write-open EROFS、readlink 投影、INIT 结构、真实 INIT+control readiness、fusermount3 SCM_RIGHTS 解析。

这些测试使用临时目录、FakeRuntime、真实 Unix socketpair 和真实本地子进程；**没有真实 FUSE mount/Podman 成功结论**，不能替代主控实测。

## CLI/API

```bash
python3 tools/unifier.py view open view-debian13
python3 tools/unifier.py view status
python3 tools/unifier.py view status <view_uuid>
python3 tools/unifier.py view readlink-raw <view_uuid> /etc/os-release
python3 tools/unifier.py view revoke <view_uuid>
python3 tools/unifier.py view recover [<view_uuid>|<env_id>]
```

Python API：`ViewManager(root, service).open(env_id)`、`status()`、`readlink_raw()`、`revoke()`、`recover()`；lease 由 `ViewRegistry` 持久化；FUSE worker 为隐藏的 `view _fuse`，storage broker 为 `view _broker`（均由 ViewManager 管理，用户不直接调用）。

## 待主控真实验收

- 三发行版 stopped fixture 的真实 FUSE mount、宿主 `cat`/Dolphin/真实编辑器读取。
- 原 absolute symlink 不落到宿主根；`readlink-raw` 与 projected link 对照。
- named volume、nested readonly bind、overlay 覆盖优先级；未知 mount 的 fail-closed。
- default rootless 与 keep-id 的 guest UID 0/1/1000/1001、宿主访问 EACCES 不被伪装成可读。
- view active 时 start/remove 拒绝、revoke 无 mount/子进程/租约残留、worker/broker 崩溃恢复、launcher SIGKILL 生命周期（若适用）。
- 生命周期真实脚本：`probe_lifecycle.py` 的 busy、worker SIGKILL、broker SIGKILL、重复 recover、外部预先 storage reference 五类；预期 busy 后 worker/broker 仍活且 held FD 可读，kill 后 recover 两次均成功，自身 refs 归零而外部 ref 保留。
- 主控先用 `view recover` 清理首轮遗留 record，再开始；不要删除 `initial-live-attempt.json`/fixtures/`live-freecad`。

未提交、未推送；未改历史 evidence 或冻结清单。
