# 恢复后的固定CID准入审核

用户已恢复本轮测试。原10个容器、固定Debian/Arch完整CID及UUID/generation已重新只读核对，均停止；Debian无活跃lease/bridge、无旧中止锁；Arch配额不合格继续阻断。当前boot ID与暂停时相同，但旧PID未重用。现有测试slice仍1GiB RAM/128MiB swap/256任务/1核，TasksCurrent=0。
单层scope的MemorySwapMax=896MiB不等于客体实际可用swap；父slice仍128MiB。新增effective_limits逐层读取memory.max/memory.swap.max/pids.max/cpu.max，严格解析并以最紧约束判定；缺失或非法明确拒绝，不能补零。宿主1GiB保留线、30秒准入和PSI/期限不变。
旧代码反例与修正后策略/清理/租约回归独立在offline-regression验证；上一轮真实systemd监督者SIGKILL触发ExecStopPost的宿主微型单元证据仍保留，未把它冒充真实客体工作流。
只对同一固定Debian做一次有界最小start/微型exec/stop准入；在首个exec前登记init/conmon和所有原始祖先限额，验证控制器、conmon和pasta在总预算内。任何拒绝保留数据并定向收尾，不转普通runtime、不改配额、不进入包或GUI。实际通过后再登记固定Debian CLI范围保护结论。
