# 入口路径修正后的 SH01–SH08

上一轮驱动把 `guarded_entry.py` 指到了 `artifacts/` 下，进程立刻退出，容器未启动。这次把入口放在本轮目录，规格放在子目录，使 `out.parent/guarded_entry.py` 指向仓库根下两级的原入口。控制器和宿主保护线不变。调用后缀 `_r5`。保护再触发即停止。
