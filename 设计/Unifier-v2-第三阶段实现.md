# Unifier v2 第三阶段 C：停止环境的只读管理投影（本机真实验收通过）

日期：2026-10-24，Asia/Shanghai。分支：`codex/unifier-v2-control`。状态：**C 已由主控完成本机三发行版真实 Podman/FUSE、Dolphin/KWrite 和崩溃清理验收；270 项回归（266 通过、4 条件跳过）。详见 evidence/unifier-v2-stage-c-2026-10-24/acceptance.md。**

## 目标与入口

为停止状态的环境提供宿主可读的只读视图，不启动应用环境，不依赖客体 shell/cat，也不直接读写 overlay upper/lower。

```bash
python3 tools/unifier.py view open ENV_ID [--mountpoint PATH]
python3 tools/unifier.py view list
python3 tools/unifier.py view status [VIEW_UUID|ENV_ID]
python3 tools/unifier.py view readlink-raw VIEW_UUID /path/in/container
python3 tools/unifier.py view revoke VIEW_UUID
python3 tools/unifier.py view recover [VIEW_UUID|ENV_ID]
```

默认挂载点位于 `.lab/state/views/<view_uuid>/mount`，权限 0700，程序创建且拒绝已有目录/符号链接接管。AF_UNIX socket 不在长项目路径下，而位于短私有 runtime 目录（优先 `$XDG_RUNTIME_DIR/unifier-views`，否则 `/tmp/unifier-view-<uid>-<uuid>`），创建时验证 owner、0700、无 symlink。

## 生命周期

- `view open` 只接受 stopped 环境；在环境 lifecycle flock 内校验 `environment_uuid`、`generation`、exact `container_id` 和当前环境记录。
- 在锁内先写 view lease 记录（state `starting`，记录 mountpoint、runtime_dir、control/storage socket、spec/log 的**计划位置**），再创建资源。这样 start/remove 不能穿过创建过程。
- `EnvironmentService.start()` 和 `remove()` 在同一 lifecycle flock 内检查 view lease；有活动 view 时拒绝。浏览不调用应用 start，不取得应用租约。
- `view revoke` 精确匹配 worker/broker 的 pid+start-stamp，并按依赖顺序清理：先对宿主 FUSE 做正常 `fusermount3 -u`；busy 时原 worker、broker、storage 引用、socket/log/journal 和 view lease 全部保留，视图仍可读，只拒绝 start/remove。只有 FUSE 确认卸载后，才终止 worker、终止真实 broker 并释放 storage 引用。绝不 lazy unmount，不按进程名清理，不递归删除。
- broker 由外层 `podman unshare` 启动，但身份以内部 Python 进程为准：broker 在 ready 元数据中返回自身 host PID/start-stamp/nonce，父进程用 `SO_PEERCRED`、pid+stamp、nonce、父进程/会话来源核对，绝不把 outer podman PID 当 broker；外层 Popen 由管理器 `wait`/回收。
- 每个 view 有一份 fsync 的 storage journal：`mounting`→`mounted`→`releasing`→`released`，失败为 `mount-failed`/`release-failed`。正常退出由 broker 释放自身一次引用；broker SIGKILL 后，只有 journal 明确为 `mounted` 且无 FUSE 使用者时才由恢复路径执行一次 `podman unshare podman umount`。`mounting`/`releasing`/`release-failed` 等不确定窗口保留 `needs-reconciliation` 并继续阻断，不伪清理；他人事先取得的 Podman mount 引用不被强制释放。
- `view recover` 处理崩溃后留下的 stale/revoking/needs-reconciliation 记录：只清理本 view 已知文件、空目录和自身 socket；若 mount 仍 busy、storage 引用不确定或文件残留，则保留记录和错误，交由下一次恢复。失败清理不覆盖原始异常。
- FUSE worker 只有完成真实内核 `FUSE_INIT`、写出带 pid/stamp/nonce 的持久 readiness 标记，并能通过 control socket 应答 `status` 后，父进程才把 view 标为 `ready`；仅看到 mountpoint 出现不算 ready。

## Storage 与挂载覆盖

- broker 进程通过 `podman unshare` 进入存储命名空间，对 exact container ID 执行 `podman mount`，把 merged root 的 `O_PATH` FD 通过真实 SCM_RIGHTS 传给宿主 worker；broker 在 view 生命周期内持有该 Podman mount 引用，退出前执行精确 `podman umount`。传 FD 不等于假定宿主具有 broker 的 CAP_DAC：宿主 worker 用自己的凭据打开文件，受限时应返回 EACCES。
- 宿主 worker 在 `/dev/fuse` 上直接实现只读 FUSE 协议；挂载选项固定 `ro,nosuid,nodev,noexec`，不使用 `allow_other`，不设置 `default_permissions`。写、创建、删除、rename、chmod/chown、truncate 等 mutation 回调返回 EROFS。
- 持久挂载按 Podman inspect 的 `Destination` 最长前缀覆盖；支持 named volume 和显式 persistent bind。冻结的 `shared_mounts`/`persistent_mounts` 必须与 inspect 的 Source/options 一致；缺失、替换、冲突或未知 bind 不退回 merged root 空目录，而是 `BACKEND_UNAVAILABLE` 或独立的不可用 capability 节点。
- `/proc`、`/sys`、`/dev`、`/run` 以及 desktop/session 挂载是 fail-closed 的 runtime capability 节点，不冒充普通持久文件。嵌套在持久 volume/shared 目录下的实际 bind 作为更具体的数据层参与覆盖；其他未知 bind 不猜测内容。
- 路径解析使用 anchored directory FD、逐段 `O_NOFOLLOW`，拒绝 `..`、symlink 中间段和最终 symlink 对文件的直接读取；源 mount/bind 的 dev/ino/mnt_id 在每次操作前复核，发现替换返回 EIO。
- absolute symlink 的 FUSE `readlink` 返回**改写后的当前投影内绝对路径**；相对向上链接同样归一化到投影根内。`view readlink-raw` 通过私有 control socket 返回原始文本并标明 `projected-link` 语义；不会把原始 `/etc/...` 交给宿主 follow 到宿主根。

## UID/GID

- broker 发送存储命名空间的 `/proc/self/uid_map`、`gid_map`；worker 结合 Podman `HostConfig.IDMappings` 组合 host→storage→guest 范围。默认 rootless 与 keep-id 都按真实映射生成 guest metadata。
- FUSE metadata 显示 guest uid/gid；宿主读取身份仍是由挂载者（host uid）拥有的 FUSE 进程凭据，不宣称拥有 namespace CAP_DAC。`/etc/subuid`、`/etc/subgid` 按用户名查找，避免把用户名记录误当数字 uid。
- 只读视图不 chown、不 relabel 任何真实数据；UID/GID 映射只影响呈现和访问拒绝，不改变底层。

## 真实验收与边界

主控完成三发行版的只读投影、卷/bind、链接、UID、修改拒绝、缺失后端、busy FD、broker/worker SIGKILL、重复恢复、外部 storage 引用保留和 Dolphin/KWrite 验收。结果及失败历史在 `evidence/unifier-v2-stage-c-2026-10-24/`，以 `acceptance.md`、`final2-gate.json` 和 `postconditions.json` 为准。

宿主 worker 没有 namespace CAP_DAC 代读。无法证明的 mounting/releasing 窗口保留 needs-reconciliation；未做宿主断电、压力或跨宿主验收。C 通过后进入 D；受管移动的源清理仍须单独证明。
