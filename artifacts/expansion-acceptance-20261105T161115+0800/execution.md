# 扩测续行准入记录

范围：先完成PRE03离线保护注入/精确覆盖审核，再对原固定Debian CID最小准入，继而共享CLI/依赖/私有GUI；Arch、媒体、跨宿主、长压按各自条件分类。
默认新增容器0；同一时刻仅一个模型或测试作业、一个测试客体。不得改旧CID/原管理根/配额/账号/日常部署，保留v0.0.3脏改动及发布物。
T1每批512MiB/96任务/1核/0swap，独立64MiB/16任务保护器，250ms采样、30秒准入、1GiB宿主保留线、每批<=300秒。
T2拟复用已存在unifier-tests.slice的1GiB/256任务/1核/128MiB swap总预算；guardian在外。只有核验容器init、conmon、控制器及全部worker实际覆盖后才放行应用动作。未知覆盖先BLOCKED；不通过Podman update或移动日常进程凑保护。
每段实机最多45分钟；CLI<=120秒/GUI<=600秒。独立保护触发后持久锁和精确收尾，不立即重试或降低门槛。
允许新增tools/reuse_test_guard.py、tests/test_reuse_test_guard.py及本轮证据/驱动；产品修复先留反例和明确小补丁。
