# 测试装置修复与剩余验收回执

2026-11-09（Asia/Shanghai）。状态：**工作树修复与定向 T1 PASS；全仓/新运行器完整实际执行 BLOCKED；扩大测试、共享测试仍 PARTIAL。** HEAD `c947077f127cf142c78796bf64d6d1542c1ab091`。没有提交、发布包重建或部署。

## 本轮改动

- 新增 `tools/offline_test_guard.py`，将已用过的 T1 独立 cgroup 装置收进维护入口；绝对工作目录/脚本路径、新回执目录、固定资源预算、身份校验与精确清理。没有设置 RLIMIT_AS，没有降低宿主保留量或 PSI 门槛。
- 修正旧保护器的结果传播缺口：子命令失败时旧装置仍返回 0，本轮 Qwen 前置失败的 command-result 与外层退出即有实例；新入口将命令失败、保护/证据失败、成功分别返回 1、2、0。原生码保留。
- 新增 `tools/test_runtime.py`；`check_repo.py`、T1 及固定实例监督器使用短 TMPDIR。T1 的 XDG runtime 同样放短目录，拒绝通过环境覆盖启用集成或接入桌面。固定实例控制器仍保留宿主 Podman 配置环境。
- 增加每个受监控 cgroup 的 memory.pressure、memory.swap.current、anon/file、回收/缺页/工作集计数。必需指标缺失明确失败；文件缓存、匿名内存、swap 不混算。现有资源阈值保持。
- 两个维护入口使用共同作业锁；保留原项目操作锁、精确 CID 绑定与原中止锁。
- FD 回归在子进程把软/硬上限限定为 64/512，对私有 320 文件树实际观察 EMFILE，再经 worker helper 读取全部 320 个文件；主测试进程限额不变。另覆盖拒绝提升、无限硬上限的内核边界。该证据没有真实挂载 FUSE。
- 更新 AGENTS、仓库维护、HANDOFF；新增运行器说明及扩大/共享两大项剩余工作台账（14 组扩大、32 条共享专项）。没有改写旧报告、旧计划和旧失败日志。

## 验证

`targeted/command.log`：**Ran 106 tests，OK，4.948 秒**。模块为 test_test_runtime、test_reuse_test_guard、test_reuse_guard_policy、test_fuse_nofile_limit、test_package_finalization、test_cli_shortcuts。原生退出码 0。

监督回执：原因 `None`；工作组采样峰值 173690880 字节、3 任务；实际固定预算及最终事件在 `targeted/receipt.json`。使用历史已验证 T1 装置执行，显式查看原生码，没有把其外层退出状态当成验收。

随后用新入口运行全仓 `python3 tools/check_repo.py`。**未启动工作进程和测试套件**：60 秒内未获得连续 30 秒低压力窗口。241 个准入样本，最低可用内存约 2682.9 MiB，full PSI avg10 最高 1.63%，结束时 0.81%，未满足启动线 0.5%。退出码 2；原记录 `full/abort.json`、`full/admission.jsonl`、`full/receipt.json` 保留。同类阶段没有自动重试，也没有放宽门槛。因此不声称全仓全绿，也不声称新入口工作进程/失联收尾已完成实机故障验收。

静态语法与 git diff --check 通过。不存在目录覆盖/失败吞成功、短路径、PSI/坏指标、cgroup 数据分层及身份更换拒绝有定向离线合同；真正的 T2/T3 仍单列。

## 委派与准备失败

最小本地 provider overlay 与 dump-config 已核验 local-server / swift-1.5-iq3_s / max。首次服务未固定工作目录，脚本路径未找到；更正为绝对路径。dump-config 的惰性 !!js 标记最初不能被 safe_load 接受，改为只读取该标记的文本，不执行它。随后 headless 缺少 LOCAL_SERVER_API_KEY，模型未执行推理或工具，未改文件，没有收费路由回退。相应原始记录及 model-summary.json 保留，由 Codex 接手。

## 保全及边界

既有 service.py、fuse_view.py、shortcuts.py、reuse_guard_policy.py 及其已有策略测试与开场摘要一致；本轮加强了 FD 测试，修改前副本在 before/。Git 索引条目、boot ID 不变，登记的各测试单元均无 MainPID。末尾只读容器清单没有运行实例；新 Arch 保持 created，其他实例 exited。本轮没有启动容器/包事务/GUI，没有改容器登记或日常入口。没有连续采集外部容器事件或对用户数据全树做摘要，不外推保全范围。

当前代码仍为工作树增量。原来未完成的运行态/Arch/RPM 包清单、通用单实例、请求桥恢复、watcher/复杂文件语义、账号设备及扩大工作流没有由本轮实现；它们明确保留在剩余台账中。
