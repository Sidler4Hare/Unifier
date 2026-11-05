# 共享容器与依赖专项测试：第一批离线结果

日期：2026-11-04（Asia/Shanghai）。结论：**PARTIAL；修复后64项离线测试全部通过，真实容器/GUI未执行**。

本轮按用户明确授权，将本轮 MemAvailable 保留线从2GiB改为 **1GiB**，未改全项目默认规范或日常产品策略。保留整树硬预算、独立监测、PSI、30秒准入、超时和新增容器0约束。

## 实际结果

- PRE02 修复前：`red-v3/command.log`，**Ran 1，失败1，退出1**。一次正式fake包事务为核验同伴清单，对停止的同伴执行了两轮 start→exec→stop。这是有效产品契约失败，不是保护失败。
- 修复后：`validation/command.log`，**Ran 64，全部通过，skipped=0，退出0**。覆盖新增8项观察合同，以及原有包事务、依赖、恢复、旧保护器离线合同和共享租约收尾。
- 验收命令：`python3 -m unittest test_package_observation test_packages test_dependencies test_package_recovery test_guarded_test test_on_demand -v`；实际环境关闭真实集成、私有HOME/XDG/TMP，工作树源入口。
- 追加3项边界检查：**NOT_RUN/BLOCKED**。60秒内未获得完整连续30秒低压力窗口，独立保护器未启动负载；原因见 `boundaries/abort.json`。不降低PSI门槛继续。新增参数化候选用例未进入最终测试文件；原候选保留在 `boundaries/proposed-test-version.py`，不能计入64项通过。

## 工作树改动与边界

`src/unifier/service.py` 不再为了观察同伴调用 start/stop/exec。只有运行时明确提供独立、最新的 `read_inventory_readonly(record)` 才读取；缺能力、读取错误、非文本或与登记不一致时，在目标启动和原生包写入前，以 isolation-preflight 明确拒绝，保留事务与隔离证据。事后独立快照发生变化仍使验证失败。

`tests/test_packages.py` 为可控fake增加读取其当前内部包集合的只读能力；`tests/test_package_observation.py` 新增8项有区分力的回归，包含停止/运行同伴、缺能力、读失败、非文本、外部包变化和外来身份。

**真实 PodmanRuntime 的只读同伴清单适配器仍未实现。** 管理根里有其他环境登记时，相关真实包事务现在会安全拒绝；没有同伴的原有正常路径保留。fake正向通过不证明真实只读数据库已实现。本轮不是已部署修复，未提交、发布或更新日常入口。

## 资源与保护

T1负载：MemoryMax=512MiB、MemoryHigh=384MiB、swap=0、96任务、1CPU；独立保护器64MiB/16任务。实际memory.max/swap/pids/cpu、PID/start和cgroup均留在各阶段receipt。保护器位于负载组外，每250ms采样；BindsTo处理保护器退出，工作进程心跳处理失联，systemd总期限兜底。T2容器保护和全部故障注入合同尚未验收，不能由T1结果外推。

本轮所有留存宿主采样 MemAvailable 最低 **2.113GiB**。64项验证阶段采样峰值约 **35.9MiB**、2个任务；最终memory.max/OOM和pids.max事件均为0，CPU节流为0。未取得memory.peak计数器，以上只称采样峰值，不报告性能SLO。

保留全部准备失败：red首轮日志创建竞态、red-v2 PSI启动拒绝、本地模型首轮凭据缺失和第二轮255秒期限未产出编辑。Qwen路由经脱敏dump核验为local-server/swift-1.5-iq3_s/max，没有远端收费回退；模型日志/路由覆盖在受限.lab目录，不含公开凭据。模型未完成后由主控接手，不把其自报当验收。

## 收尾与保全

- 容器 **10→10，运行0→0，新增/启动0**；候选Debian/Arch原登记CID匹配，登记文件哈希未变。Arch仍无合适CPU/RAM测试配额，未改旧容器。
- 本轮全部精确单元MainPID=0、负载cgroup已移除；失败单元的状态元数据保留，无自动重启。原测试slice TasksCurrent=0。没有显示、监听、真实包事务或客体进程。
- Git暂存条目一致；210个非本轮修改的源码/测试/工具/文档摘要一致；boot/KWin PID/start身份一致。源码修复前副本在 `before/`，本轮补丁在 `source.patch`，结束指纹在 `source-final.json`。
- KWin前快照采于本地委派阶段，因此身份比对只覆盖该时点至结束，不能据此声称已核验首个离线复现前后的KWin身份。
- 没有操作浏览器、活动桌面、日常容器、宿主设置或系统安装。容器前后清单一致；未采集连续事件流，日常文件未做全树摘要，不宣称所有外部事件或日常文件逐字节已核验。

剩余真实共享CLI、依赖安装/补库、Writer保存重开和Arch复核均未运行。详细32项状态见 `case-results.json`；原计划和历史回执未改写。本轮已结束，不自动重试PSI拒绝阶段。
