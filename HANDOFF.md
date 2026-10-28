# Unifier 当前接手入口

## 最新：白名单已同步日常部署并通过菜单验收（2026-10-28）

FreeCAD、腾讯会议已启用 `compatibility.mode=whitelist`，保留原 CID/镜像/包数据库、UUID/代际、应用实例、菜单、项目登记和文档。各连续两轮原菜单启动、完整窗口、正常 WM 关窗和自动停机通过；最终仅两个原日常容器，均停止、租约为空。见[本轮部署与保全报告](evidence/whitelist-daily-deploy-2026-10-28/report.md)。

真实启动发现 FreeCAD 旧 Xauthority 临时绑定路径在重启后消失。已补受限的缺失会话路径恢复，保留原容器，无 cookie 复制/输出、无已有文件覆盖；新增 6 项回归，总回归 449 项（445 通过、4 条件跳过）。腾讯会议此前 Btrfs 目录身份修复保留，数据备份在 `/home/omg/.local/state/unifier-deployments/whitelist-20261028-202845/`。腾讯会议自身的日志压缩及动态资源缓存退役已经单列核对，原件保存在备份中。

为下一台 Ubuntu Server 提供[便携源码包](artifacts/unifier-ubuntu-server-20261028.tar.gz)、[步骤](设计/Ubuntu-Server白名单与容器验收-2026-10-28.md)和 `tools/probe_headless.py`。最终包解压后去掉桌面环境变量，在 Fedora 本机三客体创建/执行/停机重开复验通过；Ubuntu 实机、GUI/媒体和既有投影开放关口仍未宣称通过。尚未提交/推送。

## 最新：发行版派系白名单与宿主—容器关系（2026-10-28）

已新增默认 `compatibility.mode=whitelist`。宿主按 Debian/Ubuntu、Fedora、RHEL、Arch、SUSE 五组精确 ID 准入，已识别但未适配的 Alpine/NixOS/Void/Gentoo、Atomic/事务式系统和未知 ID 明确拒绝；`ID_LIKE` 不授予资格。客体仍是三套已锁定的 Debian 13、Fedora 44、Arch，不把宿主家族别名当作新增客体适配器。详见[分组和矩阵](设计/发行版派系白名单与容器关系-2026-10-28.md)。

`compat list/check/matrix` 无容器副作用；`doctor` 增加白名单预检。service 创建、启动、执行及包事务检查当前宿主，新记录保存创建时关系快照，旧记录无需迁移；inspect/list/plan 显示当前关系，准入失效保留查看/停止/删除。UUID/代际/CID、客体原始 ID/版本、包驱动和共享目录契约继续严格验证。

新增 20 项契约测试；全量回归和三客体本机生命周期/原生包查询证据见[报告](evidence/distro-whitelist-2026-10-28/report.md)。原两个日常容器身份与启停时间保留，未增加常驻容器或下载。五组是机制准入，真实跨宿主、GUI/媒体和已有投影关口仍开放。当前只改工作区，未同步两个日常部署副本、未提交/推送；修改前文件的校验备份在 `.lab/backups/distro-whitelist-20261028-200931/`。

## 当前准备：复用本机已安装应用，补充同名/同类型关系用例（2026-10-27）

用户要求先查看本机已安装对象，避免重复准备，并测试同类型、同名应用的关系。已做只读包/XDG/Flatpak/关联/本地安装文件盘点，报告 [本机盘点与关系计划](evidence/installed-applications-2026-10-27/report.md)。确认宿主 WPS `12.1.2.28080.AK.preread.sw`、微信 `4.1.13.9`、VS Code/ZCode/Qoder CN、LibreOffice、MarkText、Chrome、Okular、KWrite、mpv/Haruna/Dragon 等。WPS 本地 RPM 与安装记录的 NEVRA 和不可变头摘要相同，可以直接作为 Fedora 客体同构建安装来源。钉钉、千问 DEB 和 Chatbox AppImage 是已发现安装素材，不能直接列为宿主已安装原生程序。

新增 18 组关系用例，见 `evidence/installed-applications-2026-10-27/relationship-plan.json`：Chrome 同名双入口、WPS 套件/组件/别名、Okular 同名多格式入口、办公/编辑/PDF/媒体同类型产品、同产品跨环境、RPM/Flatpak 来源、URI、PATH 与卸载/默认回滚等。先 R0 已安装宿主基线，再 Fedora/WPS 本地同构建对照，接回 Debian/WPS、Arch/Zoom 和原 9 个安装组合；Beyond Compare/飞书等新下载顺延。GUI 实际工作流均 NOT_RUN，本轮没有安装/启动应用或容器、修改默认项、读取登录凭据或改产品源码。

当前 `handlers.py` provider 合同仅 `environment-app`，通用宿主/Flatpak handler 适配是待实现关口；原生 GIO 对照与产品能力分开报告。DOCX/XLSX/PPTX 默认 WPS 总入口虽然不在此次 GIO 候选枚举中，仍须保留并实际验证，不能静默切成列表首项。当前 agent PATH 下的 soffice wrapper 不代表用户桌面 PATH；qwen 的 Wine 包装不代表原生 Linux DEB 已安装。两个日常容器基线继续维持如下。

## 最新：旧运行环境退役，腾讯会议迁移至当前代码（2026-10-27）

用户明确要求清理旧版本并迁移腾讯会议，取代此前各轮“保留全部旧容器”的运行时保全要求。已先对宿主文件和 26 个退役候选的文件系统检查点归档，60 个镜像配置及其层全部按内容校验；私有备份 `/home/omg/文档/Unifier-backups/20261027-133146-before-runtime-consolidation/`。历史证据保持原样，原根目录主状态 23 文件移入备份；历史报告中的 27 个 CID 不再代表当前活动对象。

当前只有两个日常容器：FreeCAD `ml-53c5c8a5-freecad-arch`（保留原 CID），腾讯会议 `ml-6b061a2f-meeting`（新 CID、新 UUID/代际与 app instance，keep-id、init、受管菜单）。两个应用的全部 `src/tools` 与当前工作区一致；FreeCAD 补齐了此前未部署的 5 个投影相关文件，腾讯会议保留旧安装、私有 HOME 与文档，并重新登记根/项目和带身份钉住的菜单。迁移未升级腾讯会议供应商软件包。

两个应用各两次真实菜单启动、完整窗口、正常关窗及自动停机通过；腾讯会议与项目终端同 CID/双租约、定向取消保留应用、共享文件原子保存和停机回读通过。部署端两个真实 FUSE 视图各验证 110 项目录 inode、内容大小、EROFS、活动 view 阻止启动和正常撤销。初次镜像锁配置失败保留，修正后另轮通过。旧测试容器 25 个与旧腾讯会议退役，29 个过期/临时 Unifier 镜像对象定点删除；无全局 prune，无强制删除，Pi C 等无关镜像保留。见[本轮报告](evidence/runtime-consolidation-2026-10-27/report.md)及该目录 JSON。

后续以当前源码与这两个日常环境为起点。扩展测试按下节计划复用三套有编号的临时发行版环境，执行会话共享容器；每轮结束核对 CID/租约/挂载，恢复到两个日常容器。需要故障现场时先归档，再退役，保留结果与再生脚本。摄像头/真实会议、跨宿主和既有 FUSE 通知等开放项没有因本次迁移自动通过。未提交/推送。

## 后续：扩展测试计划就绪，执行尚未开始（2026-10-27）

用户明确选择 Linux 版闭源应用，重点测试发行版和包格式限制。见[完整计划](设计/扩展可用性与Agent跨环境测试计划-2026-10-27.md)及 `evidence/extension-test-plan-2026-10-27/` 的 `plan.json`、`sources.json`、`preflight.json`。原四款 WPS / Beyond Compare / Zoom / 飞书、9 个客体安装组合保留；执行顺序按上节本机盘点调整为 R0、Fedora/WPS、Debian/WPS、Arch/Zoom，再补齐。微信宿主基线已确认，客体包及账号流程仍需独立准备；百度网盘保留候选。

并列准备 host/三客体的 12 个共享数据互写方向、三容器内 Agent 到自身/其他客体/真实宿主的 12 个目标组合、14 组 Agent 契约和 20 组文件系统语义。复用三个新隔离环境，保留日常环境、主状态及历史证据。只读 FUSE 不作互写通道；发行版不同不等于文件系统不同。guest-origin 受控入口尚未确认，P0 先盘点，缺失记 NOT_IMPLEMENTED 并补桥接后验收，不把人工宿主 relay 算成功。

本轮仅新增计划和只读预检，未下载安装包、创建/启动容器、安装/运行 Agent 或创建/挂载文件系统，未改产品源码。下一步 P0 做新备份、锁包/镜像、确认入口与执行契约，再推进三个代表应用；已有修复验收状态保持如下。矩阵所有执行单元仍为 NOT_RUN。

## 最新：投影三个边界修复通过本机真实验收（2026-10-27）

报告 `evidence/projection-boundaries-2026-10-27/report.md`，机器结论 `acceptance-result.json`，设计 `设计/投影边界修复-2026-10-27.md`。先做外部完整备份 `/home/omg/文档/Unifier-backups/20261027-102659-before-projection-boundaries/`，33,346 条目核验；5 个历史 socket 节点有元数据和已测恢复配方，不包含活进程检查点。dsh Command Code / deepseek-v4.1-flash 施工后主控接管纠错、补失败边界与真实验收；主动中断的 CLI 130 状态不代替验收。

三个问题已修：size/mtime/ctime 属性签名变化时先通知内核失效页，再回复真实属性；中间 symlink 和 `..` 按组件解析并限制在 view 根；Node.inode 使用 view/对象身份的稳定 64 位映射，和可回收 nodeid 分开。保持 cached I/O/mmap、TTL 0、原始元数据、硬链接/provider 边界和资源回收。属性版本归 Node 生命周期；真正通知失败报错并重试，内核 inode 已回收的 ENOENT 单独处理；碰撞拒绝回滚 anchor，缺失中间组件后含 `..` 的链接明确失败而不拼接或伪造目标。

最终 411 项（407 通过、4 条件跳过），66 项相关回归、真实 FUSE 核心 22/22＋边界 9/9、三客体完整链路各 26/26 共 78/78、旧契约 7/7、小预算身份与碰撞回滚通过。最终以 `*-final-v2` 结果、`accepted-source-v2/`、`source-frozen-v2.json` 为准；当前源码与实际三客体源码哈希一致。第一轮最终挂载的并发替换 ENOENT 和对应冻结源码保持原样，修后新轮次完整重跑。

默认挂载点 5 轮，持 FD 均阻止撤销；关闭后首次撤销 4/5，最终正常清理 5/5。一次约 1.89 秒 EBUSY 的现场是 3 个 dir handle，fuser 捕获 ChatGPT 外部持有者；没杀进程或强制卸载。不能据此证明历史全部原因或宣布即时撤销闭环。外部 inotify 仍无修改事件，watcher=false；性能、长压、复杂链接并发、ACL/xattr、严格快照/多卷、GUI/IDE、跨宿主开放，普通项目继续 bind。

保全 22,599 个原有文件，23 主状态、21,982 旧证据及 27 容器（含运行时间字段）均保留；仅本轮源码/测试/说明有预期变化。测试 view/容器/租约/worker/broker 和 storage 引用清理。未改日常部署，未提交/推送。

## 历史：投影复测核心回归维持，新增语义反例未修（2026-10-27）

报告 `evidence/projection-retest-2026-10-27/report.md`，机器结论 `retest-result.json`。本轮仅测试与报告，产品源码哈希与上轮验收一致。全量 399 项（395 通过、4 条件跳过），直接真实 FUSE 22/22、三客体完整 broker 51/51、契约 7/7；原子替换/unlink FD 身份、常规大小/内容变化、目录分页、预算与回收核心用例仍通过。

新边界在三次独立直接挂载和 Debian/Fedora/Arch 完整 broker 链路均复现：保留大小/恢复 mtime 的原地写入后旧 FD 缓存仍旧；`jump/../target` 中 jump 为 symlink 时被 normpath 折叠到错误目标；RELEASEDIR 回收后同一未变化条目的 d_ino 与随后 st_ino 不一致。外部修改 inotify 仍无投影事件，worker 明确 watcher=false。新增 4 项 × 3 客体全部未达到原生等价，属于语义失败而非脚本异常，不能以旧回归全绿宣布完整解决。

默认工作区挂载点连续 5 轮：持 FD 撤销均正确 EBUSY，关闭后首次正常撤销均成功。历史偶发 EBUSY 未复现但原因 UNKNOWN。独立小样本性能：stat 2000 文件约 51.2× 原生、读哈希 2000×4KiB 约 35.1×、16MiB 约 1.82×；tmpfs、5 对交替样本、未清缓存/桌面未隔离，不与 Stage F 数字直接比较。旧版控制及受 FreeCAD 启停干扰的早期样本均保留并区分。

22,198 个原有文件核验仅 README/HANDOFF 有预期更新；23 个主状态文件、21,581 个旧证据与源码保留。27 原容器身份/配置/最终状态保留；FreeCAD 09:58:24–09:59:19 的启停由用户确认来自用户或其他聊天，仅其运行时间改变，不回写。审计状态 `PRESERVED_WITH_USER_CONFIRMED_RUNTIME_ACTIVITY`。测试容器/挂载/租约/worker/broker 清理完毕，无 storage 引用残留；未提交/推送。

下一轮先修错误链接解析与保留 mtime 时的内容失效，再协调对外稳定 inode 和节点回收，随后独立验收通知。新反例脚本 `extra_probe.py` / `boundary_broker.py` 当前预期退出 1，使用新输出目录复跑；不能改写旧失败结果。长压、GUI/IDE、严格快照、多卷与跨宿主关口仍开放，普通项目继续 direct bind。

## 历史：投影对象、目录与回收本机修复通过（2026-10-26）

依据 `调研/投影文件系统架构评审-2026-10-26.md`，沿用 dsh Command Code / deepseek-v4.1-flash 施工、主控复核和真实挂载验收。先建立并校验外部完整备份 `/home/omg/文档/Unifier-backups/20261026-220929-before-projection-objects/`（31,529 条目），保留 `multilinux.fuse_view_legacy` / `view_backend_legacy`。本轮不提交或推送，不覆盖旧 F 失败证据。

节点按 provider/mount/dev/ino 持有 O_PATH anchor，OPEN 持有独立只读 FD，READ/GETATTR(fh) 对同一对象；新 lookup 辨认新绑定。目录统一 MountGraph，目录句柄持有稳定 cookie 和节点 internal 引用，FORGET/BATCH_FORGET/RELEASEDIR 与总预算、失败回滚均实现。entry/attr TTL 0，保留 cached I/O/mmap，协商 AUTO_INVAL_DATA；不是快照或 watcher 桥。相对 symlink 的硬链接别名缺少上下文时明确 EOPNOTSUPP。

最终依据 `evidence/projection-objects-2026-10-26/report.md` 和 `acceptance-result.json`：真实 FUSE 22/22、三客体完整 broker 51/51、补充反例 7/7；全量 399 项，395 通过、4 条件跳过。接受源码冻结于 `broker-accepted/project/src/`，哈希与当前源码一致。主控补修失败路径回收、资源 errno、目录账面边界和相对 symlink 别名拒绝；dsh 完成状态不代替验收。

中间一次 Debian 视图在关闭 busy FD 后立即 revoke 仍 EBUSY，后续正常重试成功；原因未证明。原记录及重试保留。随后私有临时挂载点、等待 RELEASE 处理的两轮三客体测试清理通过。最终保全：27 原容器、23 主状态文件、20,983 历史证据文件一致，无 FUSE/storage 挂载残留。

下一步仍开放：后端监听与失效/事件丢失策略、严格快照与多卷协调、READDIRPLUS/成熟协议 worker、F 性能重测与更长资源压力、复杂链接及 ACL/xattr、真实 GUI/IDE、跨宿主。普通项目继续走真实数据根/bind。恢复检查点为 `.lab/agents/projection-objects-20261026/completed.json`。

## 历史：F 本机测量完成，修复前投影一致性关口未通过（2026-10-26）

继续沿用 dsh Command Code / deepseek-v4.1-flash 协作，由主控实际执行并独立复核。报告为 `evidence/unifier-v2-stage-f-2026-10-26/report.md`，机器结论为 `acceptance-result.json`：`LOCAL_MEASUREMENTS_COMPLETE_WITH_OPEN_PRODUCT_GATES`，不是 F 全阶段通过。

Fedora N=1/2/4/8 各 3 波、三发行版混合 2 波、180 个冷/热计时样本，合计 237 个正式会话全部正常退出；111 次 Podman start 事件与预期一致。完成 270 个路径负载样本、45 次视图生命周期和 180.024 秒连续压力（2,703 轮读取、181 次原子替换）。独立 101 项证据复核及最终保全均通过。原失败/受干扰轮次全部保留，投影原 summary 的相对链接探针契约错误经三发行版真实补验确认，没有改写原失败。

发现并复现三个产品缺口：未读过的已打开投影 FD 在替换后读新文件；32→128 字节替换后立即读可能只返回新内容前 32 字节；外部修改没有投影侧 change notification。另有 12,000 路径后缓存增长，上限 UNKNOWN。真实产品 FUSE 的各负载 p50 相对宿主约 21–43 倍；共享项目继续走 bind。下一轮先修 FD 身份和大小/内容一致性，再补通知和路径缓存回收，使用新 fixture 与新目录复验。文件锁/SQLite、Git/IDE 大树、更长压力仍待做。

本轮未改 A–E 产品源码、测试、配置及日常部署。已清理本轮三个精确 CID；原有 27 个容器、23 个主状态文件、14,300 个旧证据文件与 107 个源码/测试/工具/配置文件保全，无本轮进程/租约/宿主及 rootless mount 残留。跨宿主、第二桌面/硬件及真实断电/磁盘故障仍为 PENDING_EXTERNAL_HOST。恢复与交接清单为 `.lab/agents/unifier-v2-stage-f-20261026/completed.json`。未提交/推送。

## 历史：D 完整受控移动与 E 受限闭环均已真实验收

最终源码 **359 项回归（355 通过、4 条件跳过）**，以 `evidence/unifier-v2-stage-e-2026-10-25/report.md`、`acceptance-result.json` 和 `primary-final2-*` 为准。三发行版项目会话、字面 argv/管道/文档 UID、缺失解释器无宿主回退、同 CID 租约/定向取消/worker 与代理崩溃、实际 PTY Ctrl-C 与恢复、原生 GIO 默认/文件参数/同名 app 重建拒绝/撤销及缓存拒绝、4 个撤销中断窗口均通过。同一最终源码的 C/D 真实组合回归全部通过。

E 入口：`project run NAME -- ARGV`、`project shell NAME`、`project session list/show/cancel/recover/retire`、`project export/revoke/integration`。源与 cwd/环境 UUID/gen/CID/用户/作用域固定并重验；guest PID/start/token 证明终止后才释放。撤销 intent 不会被恢复成授权。D 完整普通文件 move 需 `root --cleanup-policy controlled` + `file move --cleanup-source`，默认/旧 journal 保留源。

E 原生矩阵为本机三套 keep-id；rootless-default 身份规则仅有单元契约。全局命令拦截、任意后台 detach、压力/性能、跨宿主与 F 未做。不是逐应用沙箱，不是跨文件系统原子 move。混合安装/未知修改仍保守冲突。

日常 FreeCAD 已更新为最终代码并实际菜单启动两次、关窗自动停止，绑定/配置/菜单保持；旧代码独立备份。原有 27 个容器、23 个主状态文件、13,572 个此前证据保全。本轮 6 测试容器/3 卷已清理，正常 live-session 退役保护通过，无本轮 FUSE/storage 残留。D/E 恢复点分别见 `.lab/agents/unifier-v2-stage-d2-20261025/completed.json`、`.lab/agents/unifier-v2-stage-e-20261025/completed.json`。未提交/推送，分支 `codex/unifier-v2-control`。

## 历史：D 受控源清理与完整移动通过，准备进入 E

2026-10-25，最终 304 项回归（300 通过、4 条件跳过）。35 项原生受控移动、7 项日志窗口与重复强杀恢复、旧复制兼容/ENOSPC/CLI、三发行版 C→D 全部通过。以 `evidence/unifier-v2-stage-d-complete-2026-10-25/acceptance-result.json` 和 `report.md` 为准。API `cleanup_policy='controlled'` 与逐操作 `cleanup_source=True`；CLI `root add --cleanup-policy controlled` + `file move --cleanup-source`。旧 pending 记录无删除权，默认 move 保留源；未知归属窗口 needs-reconciliation。不是原子跨文件系统 rename，也未做断电。

原有 27 容器、23 主状态文件、13,448 先前证据文件保留。新测试容器和卷已清理。独立备份清单 `.lab/agents/unifier-v2-stage-d2-20261025/completed.json`。未提交/推送。下一步 E：项目会话、流/TTY/信号/租约和可撤销桌面入口；不得重写 D 的冻结验收。日常 FreeCAD 保持此前菜单部署。

## 历史：C 已通过，D 初版受限文件闭环已实现并真实验收

最终源码回归 **293 项（289 通过，4 条件跳过）**；C 的三发行版 FUSE/GUI/清理验收通过，D 的 20 项真实 Btrfs/tmpfs 与阶段中断、真实 ENOSPC、三发行版 C→D 复制和公共 CLI 均通过。以 `evidence/unifier-v2-stage-d-2026-10-24/report.md`、`acceptance-result.json`、`postconditions.json` 为准。未提交、未推送，分支 `codex/unifier-v2-control`。

D API `Control.copy_file/move_file/file_operation/list_file_operations/recover_file_operations`；CLI `file copy/move/status/list/recover`。move 目标复制/登记后源保留于 SOURCE_CLEANUP_PENDING；CLI complete=false 时 exit 1，不把部分完成当成功。自动源删除、完整原子移动、断电/压力/跨宿主与 E/F 尚未完成。

原有 26 个容器及状态、主项目 23 个状态文件、12,670 历史文件保持原样；B/C SHA256SUMS 均通过。3 个本次测试容器与卷已清理，无 FUSE/storage mount 残留。额外保留 1 个日常 FreeCAD，用户级菜单 ID `unifier-freecad`，项目 `/home/omg/文档/Unifier-CAD`，其代码已更新并再次实际菜单启动两次、关窗后自动停止。配置/环境 UUID/CID/项目数据保留，旧代码独立备份。

C 的接受清单 `.lab/agents/unifier-v2-stage-c-20261024/completed.json`；D 的最终接受清单 `.lab/agents/unifier-v2-stage-d-20261024/completed.json`（含备份路径/哈希）。重跑三发行版验证需新 fixture，原 CID 已清理；步骤见 D 报告，不能复用旧状态。下一工作范围是受控源清理或 E 项目会话，不能把现有保守 move 写成完整移动。

## 历史：v2 第二阶段 B 受限文件路由（2026-10-24，分支 codex/unifier-v2-control，基线 9a86520）

状态：**B 首版受限闭环与 Arch FreeCAD 真实 GUI 验收已通过；最终源码的三发行版原生容器复验也通过。** 未提交、未推送。新建测试容器已清理；原有 26 个容器、`.lab/state` 与历史证据保持原状。

- A 阶段已由监督者独立验收：三发行版真实创建/迁移/备份/退出码/绑定拒绝/数据根保留通过，证据 `evidence/unifier-v2-stage-a-2026-10-23/live-three-distros-2026-10-24.json`、`live-postconditions-2026-10-24.json`。
- Project：`project add/list/show/remove` 显式登记 root、默认保存目录、工具链环境绑定与 `edit`/`view` handler UUID；一个 root 一个 project；删除登记不删数据/包/容器。
- Handler：`handler add/list/show/remove` 首版仅 `environment-app`；固定 app 的独立 `app_instance_uuid`、环境 UUID/代际/容器实例、exec 指纹和 `save_capability`；同名同配置 app 重建也因新实例被旧 handler 拒绝；同名 handler 重建不能替换 project 授权。
- `file plan/open --path ... --intent edit|view`：用 A 的锚定 FileRef；校验 project/root/handler/app instance/环境代际/root inode/default_save_root/实际同路径挂载；按 container-path 最长挂载映射检查 root/file/cwd，拒绝 host 遮蔽、不同路径、子 ro 覆盖和 readonly policy 下的 rw 挂载。计划输出 argv/cwd/observed_version/provider/全部 UUID/save_capability/真实 environment-shared `access_scope`。
- on-demand `file open` 主入口只做只读预检，随后启动 detached `file _worker`；worker 从 path/intent 重新解析并持租约，禁止旧 plan JSON/递归监督。`start`/`exec_app` 在生命周期锁、等待结束和 exec 边界复核 app instance/执行指纹/绑定。
- 评审后补：`start()` 在 `_wait_while_stopping` 返回后重新读取 app/env 记录并复核；等待期间切换 app.env 或环境代际会在 `runtime.start` 前拒绝，保存时合并最新 record。`{file}` 必须是独立 argv 元素；中文/空格/引号/分号仍是单参数；重复 UUID/alias 和直接 API 错类型返回 `MultilinuxError`。
- 普通共享目录仍默认 `preserve-host`（无 `:z`、不 relabel）；默认保存目录只设置 cwd，`save_as_default` 为 `unknown`，不宣称应用 Save As 默认位置已生效。
- 最终回归 **236 项：232 通过、4 项按条件跳过**。新增路由回归使用临时目录 + `FakeRuntime`；真实 FreeCAD QAction/QFileDialog、launcher SIGKILL 后回收和原生包卸载后数据保留已现场通过；38 项证据检查通过。证据与交接：`evidence/unifier-v2-stage-b-2026-10-24/finalize-regression.txt`、`.lab/agents/unifier-v2-stage-b-20261024/routing-review-handoff.md`。

现场：`live-freecad/acceptance.json` 与 `validation.json`；两个失败探针尝试保留。新建默认目录只限本次先打开宿主原件再新建流程，通用 Save As 仍 UNKNOWN。

复现：

```bash
python3 -m unittest discover -s tests -v
python3 tools/unifier.py project --help
python3 tools/unifier.py handler --help
python3 tools/unifier.py file plan --help
python3 tools/unifier.py file open --help
```

该 B 检查点当时未完成：C 的停止环境只读投影/FUSE、D 的受管复制/移动/源清理/断电恢复、E 的通用无前缀工具链/桌面撤销、F 的压力/跨宿主验收。C 和 D 的后续实际进展以上方最新检查点为准。本次 GUI/卸载结果见 [验收报告](evidence/unifier-v2-stage-b-2026-10-24/report.md)。

## 定档：Unifier 架构实验基线（2026-10-23）

- 项目统一更名为 **Unifier**，私有仓库 `Sidler4Hare/Unifier`，定档标签 `architecture-experiment-2026-10-23`。本节覆盖下方历史阶段的“未提交”状态描述。
- 以 [定档说明](设计/Unifier-架构实验定档-2026-10-23.md) 为当前架构与验收范围入口；历史报告、失败尝试和冻结源码保留原文。
- 新命令入口 `tools/unifier.py` 兼容委托到 `tools/ml.py`；内部 Python 包、环境标签、MIME 和已注册应用不迁移。
- 当前机器使用 `/home/omg/文档/Unifier` 作为新目录入口；它指向原物理工作区，维持路径派生的环境身份与历史证据引用。克隆到其他目录可继续开发，但旧运行记录不能直接迁移。
- 本轮完整工作区备份放在相邻的 `Unifier-backups/`，包含未跟踪、忽略文件及 Git 历史，详见其中恢复说明；不把临时套接字当作可恢复文件。
- 后续围绕这份基线实现文件控制层，不扩大历史实验的通过范围。

## 历史：两个契约修复及 FreeCAD 真 GUI 验收（2026-10-23）

- 报告：`evidence/contract-freecad-gui-2026-10-23/report.md`；修复前四文件保存在 `before/`，本轮独立差分为 `contract-fix.patch`。没有提交。
- 创建、计划和驱动共用 `resolve_target_contract`；持久记录经 `resolve_record_contract` 校验。未知 apt 目标提前拒绝；已知目标的矛盾 system 拒绝；只有缺少 system 的旧记录可用旧字段兼容。未实现政策实际操作均 refused，不能把可描述当可运行。
- 回归 162 项，158 通过、4 跳过。新增 6 项测试涵盖多个契约字段和四种包操作，并对三套旧记录做模拟事务，核对计划与执行驱动一致。
- 使用已有本地镜像创建独立 `gui-freecad`，无 pull/包安装，network none。真 Wayland FreeCAD 1.1.4 GUI 经过 QAction 和真实 Open/Save 对话框完成：宿主原件打开、加立方体、原地保存、关闭重开、新建模型、仅输入文件名保存到当前宿主项目、正常退出自动停止、再次打开两份文件。
- 停机后回读原件 4 对象、新件 1 对象，形状有效，哈希稳定，UID/GID 1000:1000；最终 stopped、无租约/bridge/窗口，原有环境和旧模型证据保留。
- `attempt1-qt-import/`、`attempt2-dialog-submit/` 保留探针失败。最终 41 项证据检查通过；未把它们算成 41 个独立 GUI 场景。
- 新建默认目录仅验收本次先打开宿主文件再新建的流程。通用文件路由、原文件管理器集成、任意空白启动的保存默认值和并发防覆盖仍未实现。旧架构 v2 与上一节报告中的两个缺口现在以本轮修复报告为准。

## 最新：架构复核与文件控制层 v2（2026-10-23）

- [改动与测试复核](调研/Grok改动复核与架构迭代-2026-10-23.md)、[设计 v2](设计/文件与执行控制层-v2.md)、证据 `evidence/architecture-review-2026-10-23/`。
- 对比当日 09:30 源码哈希：新增 catalog，修改 routing/packages/service/cli。包驱动与更换计划已部分落地；文件层仍是设计。不要把 10 月 22 日的全部未提交改动归入此次增量。
- 本轮当前工作树回归 156 项：152 通过、4 跳过。新增 13 项文件机制检查通过；三处进程中断后重复恢复保留源，无重复登记。仅普通文件的复制发布/保守恢复，不是源删除、断电恢复或产品移动验收。
- 两项待修契约边界：新 apt 目标可通过 require_runnable，但 driver_for 只接 PREPARED；合成矛盾 system/target 记录会让计划与实际驱动不一致。未改真实环境记录。
- 下一轮按 v2 关口实施：统一目标契约与身份；宿主项目调用 Arch 应用保存；停止环境只读投影；随后补移动源清理、真实故障矩阵。必须验证宿主跟随投影绝对链接，不能只在服务端 chroot 测试。
- 本轮未改生产代码/测试/配置/工具，未重跑旧 GUI 或启停容器，未提交。原有脏工作与历史证据保留。下面各节为历史阶段，不是当前最新架构状态。

2026-10-22，本轮开发分支：`codex/screencast-protocol`，基线 `0c14564`。

用户已确认腾讯会议应用轮实测通过。本轮继续共享桌面前端、GPU DMA-BUF 与 CPU MemFd 的协议连接。M5 跨宿主尚未开始。

2026-10-22 深夜，本轮先完成生命周期与安装状态准确性修复，再做并发测试。生产代码在工作树中未提交。

## 最新一轮：生命周期与安装状态修复（2026-10-22 深夜）

用户要求先做修复与验证，顺延并发测试。两项修复均已在真机验证：

- 生命周期：`--init`（PID 1=catatonit）、受管启动器的 detached worker 租约、auto_stop 按 container_id+started_at 继承、start/stop 的 lifecycle flock 与 stopping 等待、桥接健康重建。
- 真机第二轮 `evidence/lifecycle-2026-10-22T213032+0800/`：`product_verdict=lifecycle-fixed`；20/20 冷启动通过，关窗→died p50 199 ms、记录 stopped p50 24.5 ms，快速重开 p50 884 ms，Debian/Arch 各 30 次热组后 0 僵尸，SIGKILL 启动器、并发双窗口、手动保留、3 次并发首启全部通过。
- 第一轮 `evidence/lifecycle-2026-10-22T212236+0800/` 保留 2 次 Debian 关窗测量超时作为原始失败记录；根因是共享桌面真实键盘输入触发 Mousepad 自身 Save Changes 模态框，不是产品停止流程缺陷。第二轮改用只读文档并加临时 KWin 焦点保护，同冻结代码完整重跑通过，最终判定以第二轮为准。
- 安装状态：事务新增 `maintenance`（status/items/system_integration/full_init）与 `verdict`；`pkg log` 输出 verdict，CLI 对 `verified-with-warnings` 打 stderr 警告且仍返回 0。真实 Arch 3 个事务通过：正常包 verified；合成失败钩子与真实 Sunshine 均为 verified-with-warnings（Sunshine 5 类维护失败）。证据 `evidence/install-status-2026-10-22T2108+0800/`。
- 全量回归 149 项通过（4 项 `MULTILINUX_INTEGRATION` 按设计跳过）。所有测试环境停止保留；无存活租约/桥接/窗口；用户原有腾讯会议容器保持运行。
- 代码改动：`src/multilinux/{runtime,on_demand,service,cli,packages}.py`，新增 `tests/test_lifecycle_contracts.py`、`tests/test_install_status.py`、`tools/{prepare,probe}_lifecycle.py`、`tools/{prepare,probe}_install_status.py`。
- 并发测试已于 2026-10-22 晚完成（task-4）：`evidence/concurrency-2026-10-22T220318+0800/`，`product_verdict=concurrency-verified`，44/44 设计波次通过。共享 Debian 环境 3 窗口：单容器、单 start、create=0、lease=3、进程=3；双发行版 Debian 3+Arch 3 运行中容器恒为 2；母发行版原生 3 Konsole 不产生容器；混合 2+2+2 容器=2；10 wave ×3 并发首启 stop p50 213 ms、停止中重开就绪 p50 1942 ms。S5/S7 各有一轮测量口径失败，原始记录在 `attempt1/`、`attempt2/`，修正后重跑通过，不是产品缺陷。
- 下一轮候选：Fedora 容器并发、更大 N/长时间压力、网络与 I/O 并发、M5 跨宿主；不要把本轮 `concurrency-verified` 扩展成 M5 或所有应用的结论。不要覆盖上述生命周期/安装状态/并发证据。

## 前一轮：复杂 Arch 应用（2026-10-22 20:23 后）

- 最新报告：`evidence/arch-complex-2026-10-22T202359+0800/report.md`。FreeCAD 1.1.4 原生 Wayland 的参数建模、中文文件、STEP 回读和停止后重开通过，最终真实窗口已检查，两次启动器正常退出。
- GitHub Release 的 Sunshine 原生 Arch 包完成哈希核对、本地安装和普通用户版本查询。但 udev/modprobe/systemd 钩子失败，pacman 仍返回 0，项目记 `completed`；这不是完整兼容通过。设备与串流未验收。
- 默认镜像两次下载失败，独立环境增加 USTC 镜像后恢复；保留所有失败和修正前 GUI 探针尝试。
- 独立环境 `complex-arch` 已停止并保留；宿主包、桌面入口、原项目状态核对不变。生产代码未改，已有生命周期缺陷仍在。
- 后续测试优先级见 `调研/复杂Arch应用与后续测试.md`。新增工具为 `tools/probe_arch_complex.py` 与 `tools/probe_freecad_macro.py`。

## 前一轮：启动性能与回收（2026-10-22 晚）

用户随后要求开始测性能。最新证据是 `evidence/startup-2026-10-22T182400+0800/report.md`，不是前面的 ScreenCast 功能报告。

- Debian/Fedora/Arch 的原生 Mousepad 冷、热启动各 30 次，共 180 个正式样本。冷启动 p50 约 0.871/0.969/0.891 秒，热启动约 0.592/0.672/0.586 秒；采用窗口加实际 buffer commit 的界面就绪口径，未测物理呈现。
- 正常并发租约通过，但完整回收约 8.36 秒，快速重开约 8.94–9.10 秒；PID 1 sleep 没有信号处理器，停止事件退出码 137。
- Arch 31 次连续热组打开后累计 155 个僵尸子进程。已随本轮环境停止清除。
- 三套环境强杀启动器后都丢失自动回收所有权，下一次正常受管启动也不会恢复 auto_stop。
- 机器证据有效，产品结论为 `needs-lifecycle-fixes`；105 项代码回归中 101 通过、4 跳过，不代表上述实际缺陷已经修好。
- 本轮只修改测量工具，没有修改 `src/multilinux/`。下一步先修 PID 1 的信号与回收职责、异常租约所有权，再处理重复 Podman 调用，以相同场景复验。
- 新测试环境、代理、窗口和临时 KWin 脚本均已回收；镜像和停止的实验环境保留用于复现。原腾讯会议及原始 GUI 环境保持原状态。

本轮 DSH 通道：`session-a36617c6-89f3-4e8d-82ce-8981dc67b5dd`，仍为 Command Code 的 `deepseek/deepseek-v4.1-flash`。启动器和两份任务交接在 `.lab/agents/startup-20261022T182400/`，可提交副本位于新证据目录的 `agents/`。

离线复核：`python3 tools/verify_startup_evidence.py --run-dir evidence/startup-2026-10-22T182400+0800`。全新重测使用 `tools/prepare_startup.py` 创建新证据目录；不要覆写本轮样本。旧 ScreenCast 的哈希清单对应 `8482968` 时的文件，后续 README/HANDOFF 更新属于文档漂移。

## 已有修复和部署

- Gemini 修复记录：`/home/omg/.gemini/antigravity/brain/f1790762-eb63-403a-b01c-1864b54fd49e/walkthrough.md`。
- 已验收的腾讯会议部署：`/home/omg/.local/share/multilinux/apps/wemeet/`；启动配置和新增 PipeWire 包事务在该目录的 `.lab/state/`。
- 原始摄像头失败证据是修复前记录，应保留作历史诊断。
- Grok 会话 `01a16174-992e-7cf3-9b3c-97dc91ea6f23` 完成合并和 `0c14564` 的现场证据提交。

## 本轮 DSH 子代理通道

用户指定使用 Command Code 套餐的 `command-code / deepseek/deepseek-v4.1-flash`。
启动器和调用级配置在 `.lab/agents/screencast-20261022T155040/`，不修改全局模型选择。

| 角色 | 持久会话 | 独占文件 |
|---|---|---|
| portal | `session-5c4dc75e-c116-4624-b564-7b0bf29729c4` | `src/multilinux/screencast.py`、`tools/probe_screencast.py`、`tests/test_screencast.py` |
| native | `session-15edc679-945b-4ffa-b552-fb3f7931dbb7` | `fixtures/screencast_consumer.c`、`tests/test_screencast_native.py` |
| gpu | `session-4a0b93b9-c8df-4975-9aac-21fa7ff4a924` | `fixtures/dmabuf_roundtrip.c`、`tests/test_dmabuf_roundtrip.py` |

每个角色的 `*-run.json` 记录进程、状态和会话 ID，`*-handoff.md` 记录完成后的交接。
代理退出后，可用启动器的 `--session-id` 和 `--prompt-file` 继续该代理。启动器只在进程环境中加载本机 Command Code 凭据；不要打印或提交凭据及原始推理日志。

## 本轮现场环境和复现

独立证据目录：`evidence/screencast-2026-10-22T155700+0800/`。
实验项目在其 `project/`，共享输入输出在其 `work/`，环境 ID 为 `screen-debian13`。
原腾讯会议及其登录状态保留；本轮编译依赖安装到独立 Debian 环境。

入口：

```bash
python3 tools/probe_desktop_protocols.py build --run-dir evidence/screencast-2026-10-22T155700+0800
python3 tools/probe_desktop_protocols.py capture --run-dir evidence/screencast-2026-10-22T155700+0800 --mode memfd
python3 tools/probe_desktop_protocols.py capture --run-dir evidence/screencast-2026-10-22T155700+0800 --mode dmabuf
```

抓帧通过系统 Portal 选择名为 `Multilinux Screen Share Test` 的合成窗口，记录实际缓冲类型、帧变化和色块统计，不保存屏幕图像字节。GPU 自检使用自己创建的 GBM 色块缓冲，独立于屏幕抓取。

## 当前完成状态（2026-10-22 18:01 +08:00 后）

- 三个 DSH 代理均已完成。交接报告的可提交副本在证据目录的 `agents/`；持久会话和本机启动器仍可接续。
- 真实系统窗口选择 → CreateSession/SelectSources/Start → OpenPipeWireRemote → Debian 原生消费端完整通过，经过现有过滤 D-Bus 代理，没有放宽其访问规则。
- MemFd 实际取得 60 帧 BGRx，色块通过，哈希变化 57 次。最终源码的 auto 回退复测另取得 60 帧 MemFd，哈希变化 48 次。
- DMA-BUF 实际取得 60 帧 BGRx，900×598，linear modifier，EGL/GLES 导入成功，哈希变化 56 次。使用 `glReadPixels` 核验，明确 `zero_copy=false`。
- auto 故意使用不存在的 render node，先记录 GPU 失败，再关闭原 FD、重新取得 Portal FD 并通过 MemFd，不把 CPU 成功记作 GPU 成功。
- 主机与 Debian 的私有 GBM 缓冲自检均为 7747/7747 RGB 像素匹配。
- 102 项代码回归，98 项通过，4 项旧现场测试按原规则跳过。新窗口采集由独立现场工具实际完成。
- 实验环境已停止，应用租约为空、桥接代理已撤销、合成窗口退出、Portal session/FD 均关闭。宿主包和桌面入口清单前后相同。

集成时修复了三个实际问题：Gio 的 `a{sv}` 参数必须正确构造外层 tuple；长中文项目路径下代理 socket 必须相对私有目录绑定；Debian 需要 `pipewire-bin` 提供 `client.conf`，仅有开发库和 modules 不够。构建阶段现在先检查客户端运行文件和 context 创建，再允许弹出共享框。

DMA-BUF 按 PipeWire 1.4 协议处理：EGL 导入不把 `maxsize`/`chunk.size` 当作线性内存边界，允许这些字段为 0；modifier 使用 mandatory/don't-fixate 协商，过滤 external-only 与不能证明为单 plane 的组合。MemFd 仍严格检查 offset/stride/chunk。

失败记录原样保留：首次准备因共享父目录被拒绝；前两次现场捕获因 Debian 缺少 `client.conf` 而失败。后续记录明确区分失败和复测通过。

## 后续边界

本轮只验收 Fedora 44 KDE/KOS 宿主上的 Debian 13 消费端、合成窗口和 single-plane packed RGB。实际 negotiated modifier 为 linear；非线性 modifier、多 plane、其它 GPU、monitor 选择、Portal v6 serial、长时间压力与跨宿主 M5 不应按本轮结果宣称通过。Portal v6 目前只有注入测试。

上层长期预览/编码界面仍可在这条已验证的连接上继续实现。原腾讯会议部署与其用户验收结果保留，不重开旧摄像头任务。

最终进度应以本轮 `report.md`、`validation.json`、源码哈希和实际捕获结果为准。编译、离线自检、Portal 会话握手、实际取帧、GPU 导入和跨宿主验证分别记录。
