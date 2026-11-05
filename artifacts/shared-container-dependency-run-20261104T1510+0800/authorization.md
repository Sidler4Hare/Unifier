本轮新尝试：用户 2026-11-04 明确要求“限制到仅剩1G…直接开始”，据此仅将本轮宿主 MemAvailable 保留线 R 设为 1 GiB，取代本轮 2 GiB 默认值，不改全项目文档或日常产品策略。其余 PSI、连续 30 秒准入、整树硬预算、独立监督、总期限、容器新增 0 和桌面隔离规则继续执行。
T1 作业 MemoryMax=512 MiB、MemoryHigh=384 MiB、swap=0、TasksMax=96、CPUQuota=100%，单阶段至多300秒（首次复现120秒）；独立保护器64 MiB/16任务/无swap。保护器每250ms检查，保留线1GiB连续两次、PSI超线连续2秒、内存90%或任务90%连续两次中止；任务80%停止提交。systemd BindsTo+工作进程心跳共同处理保护器退出/失联；精确InvocationID/cgroup收尾。
前次取证失败保留在 shared-container-dependency-run-20261104T1504+0800。本轮不用 RLIMIT_AS 约束Python虚拟地址空间；采用实际内存cgroup硬上限，并核验实际memory.max/pids.max/cpu.max及全部进程归属。修正点是换成符合整树预算的装置，不运行产品或容器绕过旧中止锁。
允许修改：新增本轮artifacts目录、新增 tests/test_package_observation.py；必要的PRE02修正限 src/unifier/service.py、tests/test_packages.py 及相关fake runtime，修改前单独备份。保留全部既有脏工作和Git索引。完成离线复现/直接相关回归后停止扩大；实机仍须固定池身份和实际覆盖准入。
