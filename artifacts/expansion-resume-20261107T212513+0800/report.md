# 2026-11-07 共享 CLI 复试回执

结果：**INVALID**，原因 `CONTROLLER_INVALID_RECLAIM_PRESSURE`。容器没有启动，SH01–SH08 没有新的产品结论。这是宿主 PSI 中止之后的一次复试；同一控制器保护再次触发，本阶段停止，不自动重试，也不提高配额。

## 范围

固定 Debian CID `e58754f0645ae1ff2924cb09a7264cc821e44893e7589a7fd0ebca09ce5f5e45`（`ml-2f4a1877-debian13`）。控制器使用已授权的 384 MiB / 288 MiB high / 128 任务。汇总组仍是 1 GiB / 128 MiB swap / 256 任务 / 1 CPU。监督器 64 MiB / 16 任务。HEAD `fbfbece91e859289de1cbdad5f467cc74d5301f1`。没有新建容器，没有改旧容器，没有包事务，没有 GUI。

## 样本

`samples.jsonl` 只有 2 条。第一条控制器约 22 MiB。约 250 ms 后控制器 302 MiB，其中文件缓存约 258 MiB、匿名内存约 40 MiB，`memory.high` 事件 116。宿主 MemAvailable 最低约 3.31 GiB，当时 PSI avg10 仍是 0。命令没有 `command-result.json`。`cleanup.json` 为 `NO_START`。

## 收尾

容器仍是 exited，启动时间仍是 2026-11-06T10:26:42，退出码 143。租约为空，bridge 为空。`unifier-tests.slice` 的 TasksCurrent 为 0；组上还记着约 252 MiB 文件缓存，没有存活进程。本轮 guard/work 单元主进程已退出。其他容器没有处于运行状态。停止态包库阶段没有重跑。DEP、Arch、私有 GUI 没有启动。
