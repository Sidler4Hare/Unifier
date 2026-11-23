# Unifier 当前接手入口

2026-11-23，仓库正式版本为 **Unifier v0.0.8**，CLI 版本为 `0.0.8`。下载与校验见 [v0.0.8 版本说明](docs/v0.0.8.md)，本机使用情况与待复验项见 [当前状态](docs/当前状态.md)。命令体系的进一步调整按 v0.0.9 设计推进。

## 历史交接记录（保留原文）

2026-11-14：本机日常版本 **v0.0.7**，主工作树保留 **0.0.7.dev0** 版本标记。正式包由已验收候选提升，仅更新产品版本号和版本说明。现行证据及未验收范围见 [当前状态](docs/当前状态.md) 和 [v0.0.7 版本说明](docs/v0.0.7.md)。部署、回退和旧备份清理见仓库 `artifacts/release-v0.0.7-20261114T155436+0800-EWALREto/report.md`。

- 2026-11-12 v0.0.6补丁已部署：基于日常v0.0.5保留Portal等已有修复，仅合入WPS前台退出策略。全局/FreeCAD/会议入口均为0.0.6，安装目录 `/home/omg/.local/share/unifier-code/releases/0.0.6-32223567047a`；独立发布包943项（5跳过）及安装版6项通过。发布包SHA-256 `32223567047a1c0506b8b79fb732a135287605be384e9e9548aedb5a1ac803e9`。回滚、来源差异和边界见[部署回执](artifacts/release-v0.0.6-20261112/report.md)。本工作树未整体覆盖到安装版，候选源码在本轮artifacts/source；未提交或推送。以下“尚未部署”为旧阶段状态。
- 部署保全例外：日常钉钉/meeting实例在11:18:09已停止并留下recovery-required租约，早于11:18:50准备命令和11:23入口切换。来源未确认，原状态保留，未自动恢复/重启。其余10个容器状态与65个业务元数据摘要保持；详见[独立状态变化](artifacts/release-v0.0.6-20261112/concurrent-state-change.json)。

- 2026-11-12 WPS关闭问题已修复并真实验收：前台命令退出后CEF/cloud辅助树常驻导致租约无法结束；新增显式 `--exit-policy stop-children`，默认等待行为不变，仍须全部后代退出后才释放。88项定向、906项全仓（5跳过）通过；固定Debian整合模式完成两轮编辑保存/重开及三次关闭，均TERM收尾、无KILL，CLI保留，最后租约自动停容器。SH10为PASS，PRE03仅补齐该固定负载，整体一期仍PARTIAL。配额192/256已恢复、256个登记身份退出、11个CID/原文/索引/桌面身份保持。见[最终回执](artifacts/wps-close-regression-20261112/report.md)与[最新台账](artifacts/wps-close-regression-20261112/case-ledger.json)。
- 收尾只读核验：当前日常入口实际为 `0.0.5-98f0a9546633`，早先交接中的v0.0.4为旧信息。安装版不含此次退出策略；本次未部署、提交或推送，不能把工作树及隔离验收等同于日常安装版已修复。字体/版式、云账号和其他目标仍未验收。以下旧轮次结果保留历史语义。

- 2026-11-12 用户降低负载后的最新复试：原预算准入通过，50项保护合同通过；私有粘贴首次实测成功，保存标记准确且原文保留，CLI同CID心跳持续。但第一次关闭时窗口已消失、WPS后台执行树未在25秒内退出，SH10为关闭阶段FAIL，重开/第二轮NOT_RUN。本次峰值260任务/约800MiB、宿主可用最低3.95GiB，无资源保护中止。监督器收尾后正式恢复3条租约、恢复192/256配额，44个登记身份退出、11个CID与非目标状态/原文档/索引保持。见[最新回执](artifacts/expansion-input-r2-20261112/report.md)、[关闭问题](artifacts/expansion-input-r2-20261112/close-finding.md)及[最新台账](artifacts/expansion-input-r2-20261112/case-ledger.json)。未自动重试，完整扩测仍未完成。

- 2026-11-12 私有粘贴续跑获批后的最新结果：50项保护合同通过，但GUI准入等待60秒未得到连续15秒合格窗口；可用内存最低2.08GiB，所需2.1875GiB，最长合格窗口3.26秒。BLOCKED_ADMISSION，容器/Writer/CLI/私有显示均未启动，粘贴修正未实测。配额已恢复客体192/汇总256；10个登记身份退出，11个容器状态/PID/启动时间均未变，租约/原输入/环境记录/桌面身份/索引保持。本轮未自动重试，见[准入回执](artifacts/expansion-input-20261112/report.md)与[最新台账](artifacts/expansion-input-20261112/case-ledger.json)。以下旧轮次描述保留历史语义。

- 2026-11-12 配额方案获批后的最新复试：固定Debian客体256/汇总384任务真实生效，50项保护合同通过；Writer与CLI同时运行，峰值259任务/约800MiB，无保护中止。但第一次保存的输入标记重复/漏字，SH10为输入保真FAIL、归因未定，两轮正常关闭重开未执行。正式恢复3条租约、58个登记身份退出，原配额192/256已恢复，11个CID和非目标状态/桌面身份/索引不变。下一次私有剪贴板输入脚本已准备但未执行；单次授权已用完。见[本轮回执](artifacts/expansion-tasks-20261112/report.md)、[最新台账](artifacts/expansion-tasks-20261112/case-ledger.json)和[续跑方案](artifacts/expansion-tasks-20261112/next-execution-proposal.md)。下方旧轮次状态保留历史语义。

- 2026-11-12 扩测续跑：仅修正请求桥恢复测试的dead-worker夹具并补存活会话拒绝对照，87项定向及892项全仓（5跳过）通过；产品源码未改。原固定Debian的SH10在许可确认后的启动阶段达到250/256任务，ABORTED_TASKS；尚未编辑保存重开，不能算产品FAIL或GUI通过。正式恢复3条租约，55个登记进程退出、CID停止、11个容器状态保全、索引未变。阶段结束，不自动加额重试；具体384汇总/256客体任务数方案待明确批准。见[续跑回执](artifacts/expansion-resume-20261112/report.md)、[台账](artifacts/expansion-resume-20261112/case-ledger.json)及[待批准方案](artifacts/expansion-resume-20261112/next-execution-proposal.md)。一期仍缺Arch/RPM停止态清单、同管理根/运行态快照、完整包事务等，不等于只剩扩大测试。

现行包是 `src/unifier`，用户入口是 `unifier`，诊断入口是 `python3 tools/unifier.py`。`src/multilinux` 和 `tools/ml.py` 只做旧名字转发。证据在 `archive/evidence`，根目录 `evidence` 是符号链接。已完成的阶段稿和早期调研在 [archive/notes](archive/notes/README.md)。布局见 [docs/布局.md](docs/布局.md)。

分支是 `codex/unifier-v2-control`。产品行为以仓库根 [README.md](README.md) 和 `设计/` 里留下的说明为准。先后顺序见 [远期规划](设计/远期规划-2026-11-08.md)：先做完 Debian 13、Fedora 44、Arch，再扩展其他发行版。下面带日期的条目保留各轮结束时的说法，不覆盖这个顺序。

## 接手时先记住

- 2026-11-10 后续：本机日常入口切到 v0.0.4 的 r3 快照。钉钉、腾讯会议和 FreeCAD 的应用包装跟随同一个稳定入口。包清单已核验、无需恢复且隔离检查无异常时，补库未收敛的安装才允许继续登记并保留失败退出码。 `dirs` 对照实际 CID 的绑定显示宿主和客体路径，`path --files` 只返回可用共享目录。已给原版 Dolphin 安装 Debian 的「位置」与右键复制动作。Arch 的 `~/文档/Unifier-CAD` 当前缺失。60 项定向离线检查和复制夹具通过，真实 GUI 未验收。见 [本轮回执](artifacts/dolphin-file-entry-20261110T141014+0800/report.md)。

- 2026-11-09 最新：共享专项在固定 Debian CID 上的 **SH01–SH08 全组通过**，包括三应用、启动计数、租约及手动所有权。867 项全仓检查（5 项条件跳过）及后续 49/35 项保护合同通过。SH10 首次内存中止，一次收紧客体回收线的重试到达私有 WPS 许可窗口后 PSI 中止，文档工作流仍 INVALID；两次正式 app recover 均通过。全部登记执行退出、租约/bridge 清空，11 个容器均未运行，无新增和非目标执行事件。完整 END01 缺启动前部分保全基线，仍 PARTIAL。见 [共享验收回执](artifacts/shared-acceptance-20261109T221135+0800/report.md)、[剩余工作](docs/测试剩余工作-20261109.md)及[统一运行器](docs/测试运行器.md)。工作树未提交、未部署。Arch generation 2 仅静态限额准入通过；旧 296 包摘要和升级估算不能作为新根基线。其余依赖、Arch、完整 PRE03 和扩大测试仍未收口。

- 2026-11-08：v0.0.4 在安装成功后把所选启动入口写入宿主软件列表，不改文件默认打开方式；包内标为隐藏的入口不进菜单。日常入口是 `~/.local/share/unifier-code/releases/0.0.4-b04db4836d79`，`unifier --version` 为 0.0.4。源码包 `artifacts/unifier-0.0.4.tar.gz` 的 SHA-256 是 `b04db4836d799f76369912ab68e175d0c308a5ee5c78f69191f50374579f9462`。0.0.3 目录保留，回退链接是 `~/.local/bin/.unifier-e_irnbfl.rollback`，稳定名是 `~/.local/bin/.unifier-0.0.3.rollback`。FreeCAD 与腾讯会议只改了启动包装里的代码目录，数据根没动。12:26 的中止记录在 [第一次组装](artifacts/release-v0.0.4-20261108T122119+0800/report.md)，切换细节见 [复跑记录](artifacts/release-v0.0.4-20261108T123341+0800/report.md)。这次切换不是 PRE03 或新的应用验收。

- 2026-11-07 维护：日常入口已切到当时重新组装的 v0.0.3，目录是 `~/.local/share/unifier-code/releases/0.0.3-892b1623c220`，`unifier --version` 为 0.0.3。源码包 `artifacts/unifier-0.0.3.tar.gz` 的 SHA-256 是 `892b1623c220311d9cd3b770cfcf4236070b8d0fc7bd81f3822d27c20616a032`。11 月 5 日的旧包字节在 `artifacts/backup/unifier-0.0.3-20261105/`。已安装的 0.0.1 和两份 0.0.2 代码目录移到 `~/.local/share/unifier-code/backup/`，回退链接是 `~/.local/bin/.unifier-0.0.2.rollback`。FreeCAD 与腾讯会议的数据根没动，只把它们的启动包装改到这一版代码。用户数据 `~/.local/share/unifier` 保留。这次切换不是新的实机验收。

- 2026-11-06 续测见 [本轮回执](artifacts/expansion-resume-20261106T095650+0800/report.md)。原固定 Debian CID 的最小启动/执行/停止已通过，并取得 init、conmon、pasta 和控制器的汇总 cgroup 证据；完整共享负载仍未通过。真实 APP 路径发现并修复默认空参数被拒绝、客体 HOME 覆盖宿主 Podman 环境两处问题；通过匿名 env-file 传递客体环境，保持宿主客户端配置与凭据 argv 边界。多应用复测被 PSI 保护中止，随后停止态包库探针被控制器子组回收保护提前中止，原始失败/中止锁保留。固定 CID 已停止，正式 app recover 已释放中止执行的租约。当前控制器 192 MiB/64 任务不足，扩大子预算尚未执行；不要把最小样本当作全部 PRE03/SH/DEP 放行。收尾审计 `final-audit.json` 仍是 FAIL，只有索引文件哈希不同；索引树与 HEAD 树一致，说明见该回执的 `index-clarification.json`。END01 操作收尾已记录。2026-11-06 已授权并执行一次 384 MiB 控制器的停止态包库复试，容器启动前再次因回收压力中止，见 `inventory-pilot-384/`。同一保护类不再自动复试。v0.0.3 现有源码包和日常 v0.0.2 安装版均未重建或部署这批修复。

- v0.0.3 组装：已将本分支现有增量纳入新版源码范围，并增加日志驱动的 `pkg repair-deps --from-log`；`--diagnose-only` 不访问产品环境。新版包、离线验收和原包保全以 [组装报告](artifacts/release-v0.0.3-20261105T152926+0800/report.md) 为准。组装本身没有改日常入口；2026-11-07 和 2026-11-08 已分别切换。下面带日期的回执描述各轮结束时的状态；“未提交/未构建”和旧测试数量不能覆盖本轮状态。PRE03、同管理根真实分支、目标加载器及 GUI 仍独立阻断。

- 2026-11-05 当前状态：先读 [全项目测试禁令与规范](docs/测试禁令与规范.md) 和根 [AGENTS.md](AGENTS.md)。默认不新建项目容器或管理根，不按名称杀进程，不操作用户桌面。实机须有整棵进程树硬预算、独立监测和期限。现行宿主保留线是 **1 GiB**。`tools/guarded_test.py` 仍是旧适配器：同进程线程、每秒采样、连续三次低于它自己的 2 GiB `HOST_RESERVE` 才中止，而且 `create()` 会新建容器。它不能当作 T2 放行器，旧集成用例也不能直接启用。
- 同日架构修复停在 **PARTIAL**，源码在本分支工作树。S1 管理根协调锁、S2 停止态 Debian 只读清单、S3 APP subreaper 租约与恢复、S4 bundled 候选分层，各有限定 T1 回执：[首轮](artifacts/architecture-repair-20261105T104613+0800/report.md)、[续轮](artifacts/architecture-repair-20261105T111850+0800/report.md)。运行中的同伴、Arch/pacman 和 RPM 清单明确 unsupported。存储引用释放失败会持续阻断，没有自动恢复命令。通用单实例和外部服务代启未实现。同管理根真实同伴 FIX01-05 与实机保护 PRE03 为 BLOCKED。写这段时，发布包和日常安装版还没有重建或部署。11 月 3 日的 726 项全仓检查早于这批改动。当时的下一步已被后续回执取代：最小 Debian 准入和一次停止态清单对齐已有结果，剩余门槛见上面的“仍开放”。
- 2026-11-03 续测按用户指定的 dsh 本地 Qwen 实施候选补丁，主控独立验收。新增可移植 FD 3 控制管道、凭据不进 argv 的项目会话传递、精确停止 CID 恢复和受控请求桥原型。Writer/Sheet/演示各两轮私有 GUI 保存/停机重开、Compare 选择性合并/保存/重开通过数据检查；Ubuntu 上 Fedora/Arch 六个请求组合、受限 1804 秒持久性、726 项本机回归与 Python 3.11 回归已收齐，数字属于当时的树。总体验收仍 PARTIAL。原型没有请求日志自动恢复命令，broker 也没有专属 SIGTERM/SIGHUP 收尾。见 [续测报告](artifacts/expansion-continuation-20261103T100648+0800/report.md)。这批源码随后留在本分支；日常已安装的 v0.0.2 发布物不包含它。
- 同轮发生过宿主桌面事故，根因 UNKNOWN。后续禁止重复使用旧 KWin 或宿主桌面自动化；先读 [扩测保护与恢复](docs/扩测保护与恢复.md)。那一轮适配器的门槛是宿主 2 GiB 再加客体预算，最终启动核验因低可用内存拒绝，测试容器保持停止。不得删除那次中止锁，也不得改用普通 app worker 绕过。原容器和暂存条目的备份、精确 CID 见该报告。事故记录里的 2 GiB 是当时适配器的阈值，现行项目保留线以测试禁令的 1 GiB 为准。

- 2026-11-01 续修：用户指定通过 dsh 的 `Local qwen server` / `swift-1.5-iq3_s` / `max` 负责编码。已独立验收包文件路径保真、实机默认回执保全两项本地模型补丁；另复核保留包 CID、递归补库和进程组清理修复，更新宿主准入与隔离集成测试。本轮 633 项离线检查（628 通过、5 条件跳过）及 Debian/Arch 真实有界探针通过，9 个原容器状态不变。该轮结束时只有工作树、尚未部署。身份、备份、补丁和边界见 [续修回执](artifacts/continued-repair-20261101T154240+0800/report.md)。

- 2026-11-01：v0.0.2 补齐包事务执行前日志、SIGTERM/HUP/INT、跨会话子进程树清理、超时后的清单重核验和 `pkg recover`。不明状态或原生数据库未修好会挡住下一次包变更。回执、精确构建身份、部署和回退入口见 [本轮报告](artifacts/expansion-fix-20261101/report.md)。原生中断测试用独立 `ml-26942dd5-interrupt-debian`，不属于日常环境；未检查精确 CID 前不要删除或复用。

- 2026-10-30 宿主关系策略版本 2：禁止同派系容器，包含 Ubuntu → Debian。正向测试必须过滤宿主派系，旧同派系执行记录不算设计通过；已有环境保留数据，允许查看/停止/删除，不能继续启动或执行。
- 同日补齐运行态 `ls` 检查及服务层每安装根 target 唯一性和跨进程创建锁，已承接的 target 也不能绕过；日常部署做最小准入补丁。`compat matrix` 有任一准入目标返回 0，具体目标仍用 `compat check` 判断。新版交付与验证见 `artifacts/host-authority-fix-20261030/report.md`，不改旧回执。

- 日常容器只有两个：FreeCAD `ml-53c5c8a5-freecad-arch`，腾讯会议 `ml-6b061a2f-meeting`。2026-10-31 本机扩测另保留两个已停止的 Debian/Arch 测试容器，安装状态供剩余阶段续用；这不是最终拆除。身份与恢复入口见 [本轮报告](artifacts/extension-local-20261031T111237+0800/report.md)，后续按精确身份清理。
- 容器标签、容器名、MIME、桌面 ID、容器内路径、`.ml-` 前缀、投影 inode 盐和 `MULTILINUX_*` 标记保持原拼写。探针线协议 `multilinux.*.v1` 也保持原样。
- `archive/evidence` 的字节保持原样。报告里的旧路径是归档时的路径。
- 物理工作区是 `/home/omg/文档/多兼容linux机`。`/home/omg/文档/Unifier` 指向它。路径参与环境身份，搬动物理目录会改变身份。
- 快捷命令测试按英文片段断言。本机 locale 是中文时，用 `LC_ALL=C LANG=C` 跑 `tests/test_cli_shortcuts.py`。
- 统一离线入口是 `python3 tools/check_repo.py`；提交前用 `--staged` 检查独立导出的暂存快照。归档探针复跑和代码整理顺序见 [仓库维护](docs/仓库维护.md)。

## 仍开放

- 2026-11-10 只读审计：宿主文件进客体今天只有一条 rw 通道（环境的文档 bind，例如 `~/.local/share/multilinux/documents/wemeet`，客体同路径），新设计的发行版专用 `files/` 对承接来的日常容器未绑定，`unifier dirs`/`path` 反而只报那个没挂的目录；宿主侧没有 Dolphin 动作也没有 `put` 命令。portal 一侧 `on_demand.py` 已把 `portal.Desktop`/`portal.Documents` 过滤转发进客体、宿主 KDE portal 后端齐全，缺的是路径可见性（portal 返回宿主真实路径，DocumentStore fuse 没挂进客体，Wayland 拖放同一个问题）。三条可选做法与未验证项见 [宿主文件进入客体与文件管理器集成](设计/宿主文件进入客体与文件管理器集成-2026-11-10.md)。本轮未改代码、未起容器、未做 GUI 实验。
- 2026-11-10 工作树改动与日常入口切换：自动补库不再因为一个补不出的 SONAME 丢掉同一轮里已唯一命中的 provider，选不出的库按 `dependency_repair.unresolvable` 的 `absent`/`ambiguous` 分类记录；包已装好而只有补库没收敛时 `unifier install` 仍登记入口并写入软件列表，命令保持失败退出；带点的桌面 ID（`com.alibabainc.dingtalk`）在发现阶段折叠成记录库接受的别名，`install.entry_missing` 不再把任何登记失败都说成“不可执行”。触发场景是 Debian 13 钉钉 8.3.1：Trixie 没有 `liblber-2.4.so.2`、`libldap_r-2.4.so.2`、`libpangox-1.0.so.0` 提供者，现场记录在 `~/.local/share/multilinux/apps/wemeet/.lab/state/transactions/`。日常入口已从 `0.0.4-b04db4836d79` 切到脏工作树快照 `~/.local/share/unifier-code/releases/0.0.4-wt20261110-c947077f-dirty-r2`（只含 bin/src/config/fixtures/tools），回退链接 `.unifier-8vd0uxms.rollback`→r1、`.unifier-1yb4yve8.rollback`→0.0.4，`package-offer.json` 与 `unifier-package-offer.desktop` 已同步到新快照。钉钉已进软件列表（`com-alibabainc-dingtalk-49d9ef`），但还没启动过，不能算可用。T1 定向 42 项、CLI 合同 58 项与 887 项全仓离线检查有回执，仍剩 1 项与本改动无关的失败：未跟踪的 `tests/test_agent_bridge_recovery.py` 期待 `recovered`，当天早上未提交的 `agent_bridge.py` 改成 `kept-live-session`。部署与三轮安装的过程记录在 `artifacts/worktree-deploy-dingtalk-20261110T105229+0800/`。未提交、未重新打包发布包。
- 近期按 [远期规划](设计/远期规划-2026-11-08.md) 收口三个现有目标。2026-11-07 固定 Debian CID 的停止态清单与 dpkg-query 对齐，670 个包，存储引用已释放，见 `artifacts/expansion-resume-20261107T215200+0800/`。同一次续测里 DEP01–DEP06、DEP08 和 SH09 通过，见 `artifacts/expansion-resume-20261107T222500+0800/rerun/report.md`。11 月 9 日补验 SH01–SH08 通过；GUI 资源无效样本、同管理根第二环境、运行态快照、Arch/pacman 和 RPM 清单、存储引用对账命令及单实例协议仍未收口。PRE03 已有固定 Debian 三 CLI 完整覆盖，不能外推 GUI/Arch。DEP07、DEP13、SH10、AR01–AR03 仍阻断，DEP11/12 为能力/实证部分覆盖。固定池的 Debian 与 Arch 不在同一管理根；Arch 已于 11 月 9 日按明确授权重建并通过静态限额准入，尚未启动，新根包清单和升级预算须重新核对。后续仍默认新增容器预算 0，不自动修改其他实例配额。性能、其他架构和游戏都还没有测量，排在三目标之后。
- 投影：普通项目直接 bind；只读 FUSE 只浏览停止的环境，活动视图会挡住启动。外部通知 `watcher=false`。严格快照、ACL/xattr、长压、通用 IDE 未过。普通文件页面在每次属性重验证时失效，无重验证的 mmap 读取不保证发现外部修改。
- 投影 rename 两个反例当前均通过，本机真实 FUSE 也覆盖相对链接、嵌套挂载和旧 FD。旧 Python 租约、受限链接目录列举与时间戳碰撞修复及本轮验证范围见 `artifacts/v0.0.1-20261030/report.md`。
- 受管移动只有显式源清理才删源。`handlers.py` 的 provider 仍只有 `environment-app`。
- 版本在 `src/unifier/__init__.py`，`unifier --version` 在本版源码中显示 0.0.4。发布构建入口是 `python3 tools/build_release.py`，当前源码包为 `artifacts/unifier-0.0.4.tar.gz`。升级会保留入口回退链接，步骤见 `docs/v0.0.4.md`。旧包与旧回执保留，不代替本版验收。
- 三宿主跨派系六组合的基础结果已在 `artifacts/server-cross-host-correction-20261030T210239+0800/`，不是完整应用扩大测试。扩大计划仍在 `设计/扩展可用性与Agent跨环境测试计划-2026-10-27.md`，当前修订 3 与机器矩阵在 `artifacts/extension-test-plan-20261031/`；按每宿主两个准入客体安排，旧矩阵保持历史原件。
- 2026-10-31 已获授权实施本机扩测，下载限制解除。[扩测回执](artifacts/extension-local-20261031T111237+0800/report.md) 记录 54 次共享文件回放、12 个 CLI 核心单元、4 个真实 PTY 工作流及两个本地原生包；飞书缺失 ALSA 库导致的 SIGTRAP 已修复并独立确认登录窗口。停止后 z/Z 视图核验、缺失环境误报 ready、TTY 缩放转发和本地包 Unicode 文件名均已修复；该日离线回归 597 项，592 通过、5 跳过。修复现纳入 v0.0.2，部署以 11 月 1 日回执为准，历史未部署状态不代表当前状态。GUI 编辑/保存/账号媒体、Agent 受控桥和长压仍未完成；不能把安装或窗口观察算成完整工作流。两个日常容器及原有暂存条目保留。该日结束时仓库仍有未提交改动；已纳入 v0.0.2 的范围以 11 月 1 日回执为准。
- 2026-10-31 晚：容器安装后自动补 ELF 运行库（apt-file / pacman -F / dnf）和原生安装提示兼容已实现并真实验收：Debian 飞书自动补 14 包、Arch 补 alsa-lib，复查干净，隔离不变；PTY 提示 y/n、`--yes`、`--json` 均通过。经 apt 安装时缩放不传进 dpkg 维护脚本（APT 自带 PTY），未改。见 [回执](artifacts/auto-deps-20261031/report.md)。测试客体 `ml-06a72096-deps-*`、`ml-45dc9c7d-deps-*` 已停止保留。

各轮验收数字、备份路径和失败原件在对应的 `evidence/` 目录，不在这份接手说明里重复。
