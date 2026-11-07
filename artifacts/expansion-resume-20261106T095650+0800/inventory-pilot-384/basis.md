# 停止态包库 pilot，授权后的控制器档

时间：2026-11-06，Asia/Shanghai。用户明确授权只把下一次 transient 控制器改为 384 MiB、288 MiB 回收线、128 任务。汇总 slice 仍是 1 GiB 内存、128 MiB swap、256 任务、1 核。宿主保留线仍是 1 GiB。监督者仍是独立 64 MiB / 48 MiB 回收线 / 16 任务，不放进测试 slice。

目标是 PRE02 的停止态 Debian 清单与随后一次 `dpkg-query` 对照，再停止原 CID。不新建容器，不改容器配额，不清理 `inventory-equivalence/abort.json`。旧 spec 没有 `controller` 字段，仍走 192 MiB / 144 MiB / 64 任务。

- HEAD：`fbfbece91e859289de1cbdad5f467cc74d5301f1`，本轮源码未提交。
- CID：`e58754f0645ae1ff2924cb09a7264cc821e44893e7589a7fd0ebca09ce5f5e45`，`ml-2f4a1877-debian13`，debian13，UUID `ae147c3e-3e65-41ac-ac8d-9257c9202696`，generation 1。
- 启动前只读看到该容器 exited、Pid 0；测试 slice 的 TasksCurrent 为 0；没有 `.guard-abort.json`。
- 定向测试 `tests.test_reuse_test_guard` 与 `tests.test_reuse_guard_policy` 共 30 项通过。这不是全仓离线套件，也不是实机通过。
- 本阶段最多这一次有依据的复试。控制器回收或宿主 PSI 再次触发就结束阶段，不进入共享应用 pilot。

结果：监督者约 33 秒后以退出码 2 结束。`ready.json` 证明控制器 cgroup 为 402653184 / 301989888 / 128。最后样本控制器内存 302014464 字节，匿名 49549312，文件缓存 248905728，high 事件 177；汇总峰值 315416576 字节。`abort.json` 原因为 `CONTROLLER_INVALID_RECLAIM_PRESSURE`，`cleanup.status` 为 `NO_START`。收尾时目标 CID 仍是 exited、Pid 0，测试 slice TasksCurrent 为 0，两个单元均已无控制组。
