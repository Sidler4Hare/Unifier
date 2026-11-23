# T1 离线回归回执

时间：2026-11-09，Asia/Shanghai。状态：**PASS**（仅这三处离线用例）。HEAD `c947077f`。工作树改动未提交，也没有部署。

上一轮 `artifacts/transaction-finalization-20261108T2154+0800` 没有跑成任何测试。监督进程给自己设置了 64 MiB `RLIMIT_AS`，Python 起不来。本轮没有提高限额，改用 T1 cgroup，监督留在组外。

## 用例

| 模块 | 结果 | 数量 | 耗时 | 退出码 |
| --- | --- | --- | --- | --- |
| 六个改动文件的语法 | PASS | 6 | — | 0 |
| `tests/test_package_finalization.py` | PASS | 8 | 0.795s | 0 |
| `tests/test_fuse_nofile_limit.py` | PASS | 1 | 0.000s | 0 |
| `tests/test_cli_shortcuts.py` | PASS | 55 | 9.678s | 0 |

语言是宿主 `LANG=zh_CN.UTF-8`，没有设置 `LC_ALL`。包用例走 fake runtime。`MULTILINUX_INTEGRATION=0`。

快捷命令里“入口路径必须是现有文件才保留菜单”这一支，现有 55 项没有单独断言。这轮只说明原有路径在中文文案下仍然通过。

## 保护

- 准入 120 个样本，跨度 29.843 秒。最低 `MemAvailable` 4403732 KiB。门槛 1769472 KiB。PSI `some` 与 `full` 的 avg10 都是 0。
- 实际 cgroup：`/user.slice/user-1000.slice/user@1000.service/app.slice/unifier-offline-t1-20261109.service`。`memory.max` 536870912，`memory.high` 471859200，`memory.swap.max` 0，`pids.max` 96，`cpu.max` 100000 100000。
- 峰值内存 168001536 字节，峰值任务数 3。没有 OOM，没有 `memory.high`，没有 `pids.events` 的 max。全程没有保护中止。
- 单元结果 `success`，主进程退出码 0。单元结束后为 inactive。工作进程 189700 和监督进程都已退出。

## 保全

- boot ID `b3cc7803-0a9b-4189-be9a-aa06cf601995` 未变。KWin PID 1803，启动时间仍是 2026-11-08 11:02:46。
- 结束后 `MemAvailable` 4413640 KiB。
- 容器未查询，也没有启动。桌面会话没有操作。
- 原有脏文件保留：`service.py`、`fuse_view.py`、`shortcuts.py`、`test_cli_shortcuts.py`，以及两个未跟踪测试。本轮只新增这个回执目录。私有临时目录约 4 KiB，留在本目录里。

## 不算通过的部分

这不是全仓 `check_repo.py`，不是真实包事务，也不是 PRE03、同管理根第二环境或 GUI。日常入口仍是已安装的 v0.0.4，这批工作树改动还没有进入发布包。
