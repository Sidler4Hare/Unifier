# 共享容器与依赖专项测试启动回执

日期：2026-11-04（Asia/Shanghai）。结论：**BLOCKED；实际产品测试执行 0 项**。本轮仅做 T0 只读准入核查。

已读取最新 WPS 原生/容器对照报告、2026-11-04 共享容器与依赖专项计划、用例卡、全项目测试规范与配套细则。执行顺序按最新专项计划从 PRE02/PRE03 开始，历史应用/资源结果不折算成本轮通过。

## 阻断原因

1. 初检 MemAvailable 为 1,969,272 KiB，约 **1.88 GiB**，低于 **2 GiB** 宿主保留线。按保守离线批次 512 MiB、独立保护器 64 MiB、余量 128 MiB 计算，准入要求至少 **2.6875 GiB** 连续 30 秒，尚未满足。没有启动回归、模型或实机负载。
2. PRE02 静态复核：`src/unifier/service.py:1825-1835` 仍对停止的同伴执行 start，读包清单后 stop。离线复现/修正及真实复核均未执行；相关实机包事务继续阻断，未改为缓存或关闭校验。
3. PRE03：`tools/guarded_test.py` 的旧 `HostReserveMonitor` 仍为同进程 daemon 线程、每秒采样、三次低内存后停止，缺少新规范要求的 250 ms 独立 PSI/存活监督。已有测试 slice 不等于完整保护覆盖。
4. T0 取证脚本在初始快照构造子进程时，触及其自行设置的 **128 MiB RLIMIT_AS**，以 MemoryError、原生退出码 1 提前结束。**30 秒窗口、完整改前/改后指纹均未取得**。该准备样本为 INVALID，不能计作产品缺陷；保留脚本和观察记录，没有提高限额或更换执行器重跑。

## 用例状态

PRE01：仅列出容器，完整原登记、所有权、UUID/generation 准入未完成，BLOCKED。PRE02/PRE03：BLOCKED。PRE04/PRE05、SH01–SH10、DEP01–DEP13、AR01–AR03：NOT_RUN。END01：本轮 T0 对象收尾完成；不是产品生命周期通过。

## 收尾与保全

- 当前容器清单前后观察均为 **10 个，运行 0 个**；本轮 create/start/stop/exec 为 0，没有选定或接管任何 CID。
- 只新增本证据目录。没有修改产品代码/测试/项目文档、暂存、提交、推送、部署、安装包、调整容器配额或宿主配置。
- T0 脚本已经同步退出（退出码 1）；没有创建后台工作线程、测试单元、显示、监听或包事务。旧测试 slice 当前状态见 `test-slice-final.txt`。
- 未进行日常数据全树摘要，未采集连续容器事件流；不能由清单一致推断全程无外部事件。完整源码/索引前后哈希、boot/KWin 身份变化、租约/数据库恢复和停止态容器实际 cgroup **未独立核验**，不填 PASS。
- `tracked-status-final.txt`、`index-entries-final.sha256`、`source-final.sha256` 是本轮结束快照，不能冒充改前/改后相等的证明。宿主最后内存/PSI 见 `host-final.txt`，不是连续窗口。

## 继续条件

首先满足与所选预算相称的连续 30 秒资源窗口。随后在完整进程树硬预算、独立监测和总期限下做 PRE02 离线复现/修正与 PRE03 契约验收，再核对固定池身份，方可进入真实共享 CLI、包事务和 GUI。保留现有容器、脏文件和历史证据；本轮不自动重试。

证据：`result.json`、`preparation-failure.json`、`readonly_preflight.py`、`container-final.txt`、`host-final.txt`、`test-slice-final.txt` 及结束指纹。
