# 2026-11-06 共享容器与依赖扩测续测回执

总体验收 **PARTIAL**。用户要求继续，按原固定池、零新增容器、原管理根恢复测试。完成最小真实 CLI 保护覆盖，并修复真实 APP 路径两处启动缺陷；完整共享/依赖验收没有完成。现有控制器子预算造成保护中止，未擅自提高配额或放宽 PSI。

本轮使用工作树，HEAD 为 `fbfbece91e859289de1cbdad5f467cc74d5301f1`。源码修改未提交、未推送、未部署。已有 `artifacts/unifier-0.0.3.tar.gz` 保持原字节，SHA-256 为 `9aa7885ad6e81d07ebc19d34ae82583e6cb940b4ecda0c092c98f53d24597a96`；它不包含本轮后续修复。日常入口仍应由最终只读审计确认指向 v0.0.2。

## 测试与修复结果

| 范围 | 结果与证据 |
|---|---|
| 暂停恢复与固定池 | 新鲜元数据记录共 10 个原有 CID，全部停止。Debian/Arch UUID、generation、管理根和挂载核对，备份在 `.lab/agents/expansion-resume-20261106T095650+0800/` 私有目录。见 `before.json`、`inventory-before.json`、`execution.md`。 |
| 资源祖先链修复 | 重放旧版只看 scope swap 限额的误判，新增逐层有效 memory/swap/pids/CPU 判定，缺失/非法数据拒绝。49 项定向离线测试通过，见 `offline-regression/`。 |
| 最小真实 CLI | 原 Debian CID 启动、UID 1000 微型执行、停止通过；取得 init、conmon、pasta、控制器全部在受限汇总组的身份及路径。`live-minimum/result.json`、`receipt.json`。仅证明这个小样本，不是全量 PRE03/APP/包/GUI 通过。 |
| APP 默认空参数 | 默认 `application_state_root=''` 被 FD 3 helper 错当成非法参数，实机未启动并留下待恢复租约。修复为仅程序名必须非空，其余参数保留字面空值，NUL/非字符串拒绝；已知发生于 spawn 前的验证失败明确 `execution_started=False`。3 项专用回归及相关 47 项测试通过。原失败在 `shared-cli/`，离线回放在 `app-fix-offline/`。 |
| 宿主/客体环境分离 | 下一次实机失败显示 Podman 将客体 `/home/multilinux-1000` 当作宿主 HOME。修复为匿名 memfd env-file，不覆盖宿主客户端环境，值不进 argv、无持久凭据文件；仍只把控制 FD 3 保留到客体。51 项相关测试通过。旧失败在 `shared-cli-retry/`，修复前反例由同一回归再次重放，见 `final-validation/before-fix-failure.txt`。 |
| 当前真实 APP 样本 | `shared-cli-envfix/` 中 APP 已进入原 CID，UID 1000、正确 cwd/参数/环境并写出心跳；在组用例完成前发生 PSI 中止，不能计 SH01 或应用生命周期通过。原共享输入保留在原测试 share 的 `acceptance-1106/`。 |
| 正式租约恢复 | 每次已停止失败执行均通过正式 `app recover debian13` 路径释放，runner 只允许 inspect/info，禁止 start/exec/stop；保留恢复前租约和 `exact-container-stopped` 证明。见 `app-recover/`、`app-recover-home2/`、`full-offline/result.json`。另一次私有 HOME 的恢复探针在 inspect 处拒绝，没有修改租约，失败保留于 `app-recover-home/`。 |
| 控制器保护补强 | 192 MiB/64 任务控制器增加自己的 90% 内存/任务判定、80% 任务排队线，以及 memory.high 回收提前停止，不让汇总组余量掩盖子组压力。26 项离线契约通过，见 `controller-safety-offline/`。 |
| 停止态包库 pilot | `inventory-equivalence/` 在控制器回收保护处中止，容器未启动、没有清单等价性结果。不能宣称真实存储引用获取/释放已验收。 |
| 当前全仓离线 | 最终树 **832 项，827 通过、5 条件跳过**，60.704 秒。语法 221 个 Python 文件、42 个旧模块转发及五个 CLI 入口通过，见 `final-validation/command.log`、`repository-exit.json`。该批随后审计误把 Arch 原来不存在的租约文件当必备文件，整体 command 退出 1；原始回执保留。修正审计后只重做 `final-audit-batch/`，未再跑全套。较早 `full-offline/` 的 831 项与本次重叠，不相加。 |

## 中止与边界

真实共享样本记录 `ABORTED_PSI`：宿主最低 MemAvailable 约 4.53 GiB，汇总峰值约 367.6 MiB/37 任务，最后 full avg10=12.68%、some=13.64%。保护按阈值终止，目标停止、控制器退出。memory.events 的累计 OOM/max/pids 数含本轮开始前的旧事件，样本中的本轮增量为零，不能把历史事件当作本轮新 OOM。

控制器补强后的包库 pilot 记录 `CONTROLLER_INVALID_RECLAIM_PRESSURE`：控制器约 143.9 MiB，memory.high 事件 156；汇总峰值约 339.5 MiB，宿主 PSI 尚为零时已停止。没有扩大负载或继续运行依赖探针。这个结果证明提前拒绝生效，同时表明当前 144 MiB 回收线/192 MiB 硬限额不足以承受所用控制链；它不是产品性能失败结论。

原始中止锁、日志、失效心跳和失败租约备份均保留。包事务、安装工具链、fixture 编译/下载、GUI、摄像头、音频和活动桌面未在本轮启动。`dependency-probe/` 只准备了脚本，**未执行**。不应将文件存在当作 PASS。

## 尚未完成

独立 [32 条用例台账](case-ledger.json) 保留原矩阵定义，原始计划不覆盖。DEP09/10 只接受离线拒绝/有界失败契约；宿主私有 loader fixture 的机制证据不能代替 Debian/Arch 加载验收。

- SH01–08 全部生命周期、多 APP 同 CID、最后租约停止与手动所有权还没有整组通过。两处已修产品缺陷不等于真实组验收完成。
- PRE02 的真实同管理根第二个合规环境不存在；停止态 Debian 包库等价性和实际引用释放仍未验收。固定 Debian 与 Arch 的管理根不同，不迁移登记或新建根凑通过。
- DEP01–08、DEP11/13 的原生包事务、自然缺库、可信唯一提供者、插件、共享库卸载/升级、锁竞争与中断恢复仍未形成新实机闭环。目标 loader/ABI 验证仍缺真实客体 fixture。
- SH10 私有 Writer/CLI 保存重开未开始；老 GUI 结果保留历史范围。Arch 原实例缺 RAM/CPU 限额，AR01–03 BLOCKED；不自动改旧容器。
- 通用单实例/外部服务代启、运行态同伴快照、Arch/RPM 只读包库、存储日志对账等功能缺口仍在。

## 下一步与收尾

具体 [控制器调整方案](next-execution-proposal.md) 已写好但未执行：仅拟把 transient 控制器改为 384 MiB/288 MiB 回收线/128 任务，汇总 **1 GiB/128 MiB swap/256 任务/1 CPU**、独立监测器、宿主 1 GiB 保留量及全部压力门槛保持。项目测试规范第 4.2 节要求增加子预算先得到明确授权；未获授权前不继续同类实机尝试。该方案不能解除同管理根 fixture 和 Arch 的独立阻断。

最终保全和运行状态以 `final-audit.json`、`final-audit-batch/receipt.json` 为准；后者同时证明最后的离线控制器退出。该审计整体为 FAIL，唯一失败项是 `index_unchanged`。随后只读复核见 `index-clarification.json`：HEAD 仍是 `fbfbece91e859289de1cbdad5f467cc74d5301f1`，索引树与 HEAD 树同为 `27e11768e4bdf7f4747e2ccac3908c61757bbd5f`，没有暂存路径。索引文件 SHA-256 与开场快照不同，原因是之后的 `git status` / `git write-tree` 刷新了 stat 缓存。没有开场索引副本，不能按字节还原，也不重置索引。原 `final-audit.json` 保持 FAIL，没有改写。

END01 按操作收尾记 PASS：全局仍是原来的 10 个 CID，全部 exited；抽查的本轮单元 MainPID 为 0、控制组为空；`unifier-tests.slice` 的 TasksCurrent 为 0；日常入口仍指向 v0.0.2。这次复核没有启动容器。

只核验本轮已冻结的数据与身份，不从元数据检查推断所有未读取的用户文件内容。测试注册项和私有输入保留，便于复现；不删除历史容器或中止锁，不清理日常数据。用户随后授权了这一次 transient 控制器调整。`inventory-pilot-384/` 使用 384 MiB / 288 MiB 回收线 / 128 任务，实际 cgroup 限额与 spec 一致。控制器在容器启动前达到约 288.0 MiB，其中文件缓存约 237 MiB、匿名内存约 47 MiB，`memory.high` 事件 177，保护原因仍是 `CONTROLLER_INVALID_RECLAIM_PRESSURE`。当时宿主 memory PSI full avg10 为 6.17。容器保持 exited，命令没有结果。这是同一保护类的有依据复试，阶段就此结束，没有进入共享应用 pilot，也没有再提高配额。同管理根第二个合规环境、Arch 配额和私有 GUI 仍各自阻断。
