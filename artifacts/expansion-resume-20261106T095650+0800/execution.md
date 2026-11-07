# 扩测恢复执行记录

用户2026-11-06明确“继续吧”。先只读核对，不复用旧PID；当前boot ID与暂停时相同也重新准入。默认新增容器0，仅原固定Debian CID；Arch不满足配额时不运行；不操作日常桌面/账号/设备。
本轮允许修复测试装置tools/reuse_test_guard.py及针对性测试，保留之前全部脏文件、暂停回执和v0.0.3发布包。模型上轮多次有界任务未交付，保护器关键判定由主控接手，小型确定性代码直接实现。
T1每批512MiB/96任务/1核/0swap，独立64MiB/16任务监督，250ms采样，30秒准入，1GiB宿主保留线，每批<=300秒。
T2沿用既有unifier-tests.slice的1GiB/128MiB swap/256任务/1核；不改容器或全局限额。先离线复现单层限额误判，再校验真实祖先链，经过受限准入后推进共享CLI/依赖。每段实机<=45分钟，单CLI用例<=120秒。保护触发不自动重试、不放宽阈值。

真实APP测试随后发现两处可复现的产品启动缺陷，修复范围为 src/unifier/exec_control.py、src/unifier/runtime.py、tests/test_exec_control_empty_arguments.py 和 tests/test_runtime_control.py；旧源码副本及原失败保留。补强控制器子组保护涉及 tools/reuse_guard_policy.py、tools/reuse_test_guard.py 及相应离线测试。未更改192MiB/144MiB/64任务控制器配额、1GiB汇总预算或原容器配置。

PSI中止后通过正式app recover确认原CID已停止再释放本轮租约，不清除中止锁。新的停止态包库pilot因控制器回收提前中止，没有得到结果，不继续依赖准备或GUI。后续配额提案只写入文件，未执行，等待本次明确授权。最终离线832项（827通过、5跳过），收尾仅只读核对。

续接时补做 END01 只读收尾。原 final-audit.json 保持 FAIL，失败项只有索引文件哈希。索引树与 HEAD 树一致，10 个原 CID 仍全部停止，本轮单元已退出。没有提高控制器配额，没有新建容器，没有重跑全套离线检查。

用户随后明确授权一次 transient 控制器调整：384 MiB、288 MiB 回收线、128 任务。默认档不变。控制器契约测试 30 项通过后，对原 Debian CID 做了 `inventory-pilot-384/`。实际限额与授权档一致，但控制器在启动容器前因文件缓存跨过 288 MiB 回收线而中止，原因码 `CONTROLLER_INVALID_RECLAIM_PRESSURE`。同一保护类不再复试，共享应用 pilot 未启动。原容器仍停止，本轮单元已退出。
