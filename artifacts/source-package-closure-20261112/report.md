# 发布源码统一与三目标包管理收口回执

日期：2026-11-12。源码统一完成；三目标包管理实现及停止态实库对齐已完成，完整真实事务矩阵 PARTIAL。日常仍为 v0.0.6，本轮没有提交、部署、打 tag 或推送。

## 源码与构建

已核验 v0.0.6 发布基线 292 项，补齐主树缺失的 Portal/启动/宿主共享生产模块，保留主树原有测试与未发布工作；开发版独立标为 0.0.7.dev0。构建记录逐文件摘要/模式、源树摘要、来源 Git HEAD 和发布基线，独立解包检查候选。`source_kind=working-tree` 不代表 Git HEAD 包含全部开发改动。

首轮统一源码包检查 998 项（5 跳过）；包管理候选检查 1016 项（5 跳过）。随后新增两项远端固定实例预算合同、两项 RPM 实库边界和两项 Arch 恢复锁回归。最终候选检查结果另记本页末尾，不以早期包冒充最终快照。

## 本轮真实矩阵

| 用例 | Debian 13，本机固定 CID | Fedora 44，Ubuntu 既有 CID | Arch，Ubuntu 既有 CID |
|---|---|---|---|
| 三 CLI 完整共享 PRE03 | PASS | PASS | PASS |
| 停止态原生清单等价 | PASS，691 条 | PASS，153 条 | PASS，139 条 |
| 正式全量升级 | PASS | PASS | INVALID，客体硬限额 |
| 仓库安装 / 删除 | PASS，hello | PASS，hello | BLOCKED |
| 不存在包安装的失败收尾 | PASS | PASS | NOT_RUN |
| 本地包 1.0→2.0→删除 | INVALID，第一步 PSI 中止 | NOT_RUN；同伴 Arch 锁阻断 | BLOCKED |
| 中止后正式恢复 | PASS，dpkg 审计和清单调和 | NOT_RUN，中止样本未构造 | BLOCKED，遗留 db.lck |

三 CLI 各验证同一完整 CID、独立上下文、租约 3→0、前两次退出不停止其他应用、最后租约自动停止。不外推 GUI。Fedora 成功事务对同管理根停止态 Arch 实库前后观察一致，未唤醒同伴；宿主包和入口观察不变。运行态同伴一致快照仍不支持。

Fedora 的首轮事务脚本误把“删除不存在包”的原生成功 no-op 当作应失败；保留该脚本 FAIL，不抹掉原始结果。三个正向事务自身 PASS，负向验收改用真正不存在包的安装，独立执行 PASS。

## 实测修复与中止

- Fedora 真库包含 1,402,996 字节原生头，修正过小的 1 MiB 单头限制为有界 4 MiB；数据库总输入仍 32 MiB，测试硬预算未变。旧 gpg-pubkey 缺 ARCH 按原生查询输出 `(none)`，普通包缺 ARCH 仍拒绝。两项修复均有原生查询回归和真实等价证据。
- Arch 升级触发 `INVALID_HARD_LIMIT`：384 MiB 客体产生 28 次 memory max 事件，未观察 OOM；汇总峰值 473,333,760 字节，宿主可用最低 125,687,881,728 字节。没有提高预算或重试升级。
- Arch 首次恢复中 pacman -Dk 返回 0，但 db.lck 仍在，旧入口误报 recovered；停止态读取器正确拒绝。已修复原生锁检查、失败持久屏障及旧成功状态重审，70 项包管理回归通过。实际锁保留，不删除数据库锁；真正恢复与后续写入保持 BLOCKED。
- 素材上传及 Debian 本地包阶段分别被本机 PSI 中止。上传只到达两份驱动，未完整传包；取消后续传输和本机实机工作流重试。Debian 中止事务 txnf62b2f1930a0 的正式恢复 PASS，dpkg --audit 无异常，库存摘要恢复为 `74f651784f02216d9b94911b9e1c9d58bec439291358cdf530c065b983af847b`；这不等于本地包验收通过。
- 本地 Qwen 有界委派在 300 秒退出，未改文件；没有再次调用或转付费模型。源码与修复由主控审查完成。

## 保全、证据和剩余项

原始备份、完整日志、源码包、fixture 及本机保护回执位于 `/home/omg/.local/state/unifier/maintenance/source-package-closure-20261112/`。远端原始证据位于 `/home/agent/unifier-package-closure-20261112/`；精简台账见 [case-ledger.json](case-ledger.json)。固定身份、预算及每次尝试见 [计划](plan.md)。

保留所有中止锁、事务、租约和历史回执。没有新建容器、迁移管理根、安装宿主 RPM 系统包、改变日常桌面或调整旧容器限额。Ubuntu 的 rpm Python 依赖仅按 apt 摘要下载并私有解包。

剩余：Arch 原生锁恢复方案与现有 384 MiB 装置的完整升级预算问题；三目标本地包矩阵；签名/网络/维护脚本失败与主动中断矩阵。主动故障注入仍需测试规范的独立方案，不以 fake 或这次保护中止概括全矩阵。

当前产品状态见 [当前状态](../../docs/当前状态.md)、[实现边界](../../docs/三目标包管理收口-20261112.md)。最终构建及收尾证据如下。

Arch 锁修复已在原实例验证：正确拒绝恢复并阻止后续改包，`package_recovery_required=true` / inventory unknown；此为拒绝契约 PASS，真正恢复仍 BLOCKED。远端五个原容器全部停止、实机测试单元为空、两个目标租约为空。本机原 11 个容器保留（10 exited、既有 Arch generation 2 为 created），目标 Debian 停止、租约为空、恢复屏障已清除。后续具体条件见 [未闭环方案](blocked-actions.md)。

本机最终打包因 PSI 保护中止，未产生最终候选；保留原回执。最终源码转至现有 Ubuntu 私有目录，以同样 T1 512 MiB/96 任务/300 秒和独立监督验证；313 项内容/模式均与本机镜像清单一致。原本机源码树摘要为 `00ebcb6baf55ff29177120c0f641aa014b5a8caf9c065e9890919b2dd4c8d1cd`。

## 最终候选验收

Ubuntu 私有依赖修正后的最终独立解包检查：1009 项，6 条件跳过，其余通过；313 项 manifest、257 项 Python 语法、42 个历史模块别名及五个 CLI 入口检查通过。T1 原生退出 0、无保护中止，cgroup 和临时目录已清除；远端本轮所有 guard/work 单元均退出。该宿主的条件跳过保持如实记录，不把数目与 Fedora 宿主旧轮次直接等同。

候选 `final-0.0.7.dev0-ubuntu.tar.gz` 的 SHA-256 为 `46d64e56bf3bf2f8169dbbd03e54ff4b20a9286f8852f21142f602363bda808b`，源树 SHA-256 为 `00ebcb6baf55ff29177120c0f641aa014b5a8caf9c065e9890919b2dd4c8d1cd`。远端没有 Git 目录，包 manifest 的 source_revision 如实为 null；旁置镜像清单记录本机原 HEAD `c947077f127cf142c78796bf64d6d1542c1ab091`，逐文件内容/模式证明与本机最终开发树一致，不能把该 HEAD 当作已提交这些修改。

全量离线输出中可见 fake 包事务/拉取提示，不代表真实容器动作。Python 3.12 的 fork/短命子进程 ResourceWarning 保留在原日志；保护器最终证明工作 cgroup 已清除。源码统一、源码包验收、实机事务和部署分别判定，三目标完整实机闭环仍为 PARTIAL。

候选包已完整取回本机：`/home/omg/.local/state/unifier/maintenance/source-package-closure-20261112/final-candidate/final-0.0.7.dev0-ubuntu.tar.gz`（977,101 字节），SHA-256 与远端验收包一致，归档内 manifest 与本机 313 项源码清单一致；旁置 manifest、SHA256SUMS 与 verification.json。单归档取回 T1 PASS，工作 cgroup/临时目录清除。首次 45 秒传输超时的部分文件另存并明确不交付。

最终保全审计：Git HEAD 与原索引摘要未变，已安装 292 项文件零差异、日常入口未变，原文件无预期范围外改动。本轮本机测试单元为空，既有 11 个容器保持停止/created；Debian 已恢复，远端 Arch 保持显式恢复屏障。未执行备用归档重现脚本；没有追加无目标复测。
