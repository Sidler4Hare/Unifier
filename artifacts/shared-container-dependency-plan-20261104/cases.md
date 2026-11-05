# 共享容器与依赖专项用例卡

日期：2026-11-04。共 32 条，全部 NOT_RUN；用例卡是执行要求，不是通过回执。

[完整计划](../../docs/共享容器与依赖专项测试计划-20261104.md) · [机器可读矩阵](plan.json)

所有真实执行受 PRE02 阻断项、固定池和本机保护规则约束。离线/真实分支分别出结果；缺失条件不成立时不得计作真实补库覆盖。

| ID | 优先级 | 场景 | 执行模式 |
|---|---|---|---|
| PRE01 | P0 | 固定容器池与身份核对 | `readonly` |
| PRE02 | P0 | 其他环境包清单核验不得唤醒停止实例 | `offline_then_targeted_live` |
| PRE03 | P0 | 统一准入、资源监测和所有工作进程保护 | `readonly_and_offline` |
| PRE04 | P0 | 正常入口、工具和测试材料就绪 | `readonly_and_offline` |
| PRE05 | P0 | 备份、状态基线与受控恢复材料 | `readonly` |
| SH01 | P0 | 同发行版多应用串行复用同一实例 | `live_debian` |
| SH02 | P0 | 同容器三个轻量应用并发 | `live_debian` |
| SH03 | P0 | 关闭一个应用不影响其他租约 | `live_debian` |
| SH04 | P0 | 最后受管应用结束后的自动停止 | `live_debian` |
| SH05 | P0 | 同一可执行文件的不同调用上下文 | `live_debian` |
| SH06 | P0 | 短命启动器被结束时租约仍由真实工作进程持有 | `live_debian` |
| SH07 | P0 | 手动启动环境的停止所有权 | `live_debian` |
| SH08 | P0 | 启动失败、错误身份和未知状态的局部回收 | `offline_and_live_debian` |
| SH09 | P0 | 同容器包事务锁竞争与应用共存 | `offline_and_live_debian` |
| SH10 | P1 | 真实 Writer 与 CLI 共用实例并有界重复 | `live_debian_gui` |
| DEP01 | P0 | 原生声明依赖经正式入口安装 | `live_debian` |
| DEP02 | P0 | 未声明 ELF 运行库真实自动补装 | `live_debian` |
| DEP03 | P0 | 只在插件中出现的 DT_NEEDED | `live_debian` |
| DEP04 | P0 | 多个应用复用同一提供者 | `live_debian` |
| DEP05 | P0 | 重复 repair 的无新增依赖结果 | `live_debian` |
| DEP06 | P0 | 移除应用 A 不破坏应用 B 的共享库 | `live_debian` |
| DEP07 | P1 | 共享提供者兼容升级及不兼容版本边界 | `offline_then_live_fixture_if_ready` |
| DEP08 | P0 | 运行时构造库名与精确 library 提示 | `offline_and_live_debian_if_missing` |
| DEP09 | P0 | 无提供者或多提供者时不猜包 | `offline` |
| DEP10 | P0 | 索引错误、安装失败和补装无效的有界退出 | `offline` |
| DEP11 | P0 | GIO 等外部命令和 GUI 能力缺口 | `offline_and_live_debian` |
| DEP12 | P0 | 实际搜索路径、符号版本、架构与厂商配置的分类 | `offline_then_private_fixture` |
| DEP13 | P1 | 补库包事务中断与同一实例恢复 | `offline_then_live_debian_fixture` |
| AR01 | P1 | Arch 同实例轻量应用复用 | `live_arch` |
| AR02 | P1 | Arch 依赖与共享提供者最小复核 | `live_arch` |
| AR03 | P1 | Arch keyring、整体升级和交互预算准入 | `readonly_then_live_arch_preparation` |
| END01 | P0 | 固定池、数据、服务和资源收尾 | `readonly_and_owned_cleanup` |

## PRE01 — 固定容器池与身份核对

优先级：P0；模式：`readonly`；初始状态：`NOT_RUN`。
前置：无；END01 为 finally 必执行。

步骤：

1. 只读列出全局容器和环境登记，核对候选 CID/标签/管理根/UUID/generation/挂载/初始状态。
2. 将 Debian、Arch 候选写入唯一执行清单，冻结非目标 CID 列表及容器数量。

观察点与通过条件：

- 最多 2 个既有候选、同发行版只选 1 个；本轮新增预算为 0。
- 登记或所有权不完整即 BLOCKED，不自动收养、迁移、替换或启动其他历史容器。

结论范围：`instance-ownership`。原始动作、状态和功能证据缺失时不填 PASS。

## PRE02 — 其他环境包清单核验不得唤醒停止实例

优先级：P0；模式：`offline_then_targeted_live`；初始状态：`NOT_RUN`。
前置：PRE01。

步骤：

1. 在 fake runtime 中登记目标 A 和停止的同管理根同伴 B，调用正式包事务，记录全部 start/stop/exec 调用，重放当前 _probe_inventory 行为。
2. 完成修正后复跑：通过受支持的只读数据或明确未核验结果保留隔离契约，不禁用校验或移除 B 记录。
3. 只有存在安全完整的同管理根同伴记录时才做一次真实定向复核；否则实机分支 BLOCKED。

观察点与通过条件：

- 非目标 B 不出现 create/start/restart/exec 调用或事件。
- 无法独立读取时必须报告未知/阻断，不把缓存冒充实时核验。
- 当前实现的已知唤醒路径未修复前禁止全部实机包事务。

结论范围：`package-observation-side-effects`。原始动作、状态和功能证据缺失时不填 PASS。

## PRE03 — 统一准入、资源监测和所有工作进程保护

优先级：P0；模式：`readonly_and_offline`；初始状态：`NOT_RUN`。
前置：PRE01, PRE02, PRE05。

步骤：

1. 核对旧测试限额、真实 cgroup 祖先、控制器/应用工作进程路径和独立保护器。
2. 离线注入低内存、PSI 超线、身份不符、指标不可读、控制器失联和总时限，验证只停止本轮确证对象并阻断队列。

观察点与通过条件：

- 宿主 2 GiB 保留线与压力门槛不自动降低，生产 CPU/RAM 策略不改变。
- 失联有独立清理路径；应用自动重启不能绕过停止锁。
- 旧 768 MiB/192 任务不足时记 INVALID_RESOURCE_LIMIT，不宣称产品失败。

结论范围：`test-protection`。原始动作、状态和功能证据缺失时不填 PASS。

## PRE04 — 正常入口、工具和测试材料就绪

优先级：P0；模式：`readonly_and_offline`；初始状态：`NOT_RUN`。
前置：PRE01, PRE05。

步骤：

1. 冻结工作树入口、源码/fixture 哈希、CLI 参数、工具路径、原生包格式和目标架构。
2. 确认 fixture 通过正式安装/修复入口；对缺库用例预先冻结真实缺失条件和可信唯一提供者。
3. 预检私有显示、采样器、独立 HOME/XDG/TMP/IPC 与 GUI 能力；工具编译和下载单独执行。

观察点与通过条件：

- 不混用旧发布版、直接解包结果或手工补装结果冒充自动修复。
- 工具/材料不全先 BLOCKED，不先启动 GUI 再猜依赖。
- 源码/fixture 改变时生成新执行指纹。

结论范围：`execution-provenance`。原始动作、状态和功能证据缺失时不填 PASS。

## PRE05 — 备份、状态基线与受控恢复材料

优先级：P0；模式：`readonly`；初始状态：`NOT_RUN`。
前置：PRE01。

步骤：

1. 记录并备份选定测试根的登记、包清单、测试共享输入、账号配置边界和本轮专属文件清单。
2. 记录宿主安装指纹、非目标容器和 Git 索引；确认本轮只用新测试用户配置。

观察点与通过条件：

- 不覆盖已登录账号、日常文件与既有脏修改。
- 备份和恢复材料无法取得时，不执行包变更/故障注入。

结论范围：`preservation`。原始动作、状态和功能证据缺失时不填 PASS。

## SH01 — 同发行版多应用串行复用同一实例

优先级：P0；模式：`live_debian`；初始状态：`NOT_RUN`。
前置：PRE02, PRE03, PRE04, PRE05。

步骤：

1. 通过正式 app 注册 A/B，绑定同一个 Debian 环境。
2. A 冷启动后保持存活，再热启动 B；记录环境登记、CID 和分段事件。

观察点与通过条件：

- 相同管理根/发行版/UUID/generation/完整 CID，无新增容器或环境记录。
- 冷启动组 start 恰好 1 次；B 热启动新增 start 为 0；准备阶段启动另计。

结论范围：`same-distro-reuse`。原始动作、状态和功能证据缺失时不填 PASS。

## SH02 — 同容器三个轻量应用并发

优先级：P0；模式：`live_debian`；初始状态：`NOT_RUN`。
前置：SH01。

步骤：

1. 在同一受管共享组运行最多 3 个轻量 CLI，每个具有独立 token、argv、cwd、env 和输出。
2. 观察同时存活区间并写入各自校验文件。

观察点与通过条件：

- 所有客体 PID 归属同一 CID；实际 token/参数/输出不混用。
- 容器 start 仍为 1，无第二个测试环境启动；所有结果可独立校验。

结论范围：`shared-concurrency`。原始动作、状态和功能证据缺失时不填 PASS。

## SH03 — 关闭一个应用不影响其他租约

优先级：P0；模式：`live_debian`；初始状态：`NOT_RUN`。
前置：SH02。

步骤：

1. 正常结束 A，保留 B/C。
2. 观察 B/C 的 PID/start、心跳、租约和后续写入。

观察点与通过条件：

- 只移除 A 的租约，B/C 仍可实际工作。
- 容器和共享桥不误停；没有重建应用实例掩盖中断。

结论范围：`lease-partial-release`。原始动作、状态和功能证据缺失时不填 PASS。

## SH04 — 最后受管应用结束后的自动停止

优先级：P0；模式：`live_debian`；初始状态：`NOT_RUN`。
前置：SH03。

步骤：

1. 从停止状态形成受管启动组，确认所有权归该组。
2. 依次退出最后真实应用，观察租约、桥和容器状态；有界等待后报告结果。

观察点与通过条件：

- 最后租约释放后该组自动启动的同一 CID 停止，无新 CID。
- 清理失败/未知不删租约伪造干净状态；剩余进程与退出原因完整。

结论范围：`lease-final-release`。原始动作、状态和功能证据缺失时不填 PASS。

## SH05 — 同一可执行文件的不同调用上下文

优先级：P0；模式：`live_debian`；初始状态：`NOT_RUN`。
前置：SH04。

步骤：

1. 使用同一 CLI 可执行文件运行两个独立调用，设置不同参数/cwd/env/token。
2. 同时核对各自输出和关闭后剩余实例；GUI 单实例行为不在此用 PID 数量强行判断。

观察点与通过条件：

- 两次调用复用 CID，调用环境不交叉覆盖。
- 退出任一个调用不终止另一调用；测试 HOME 独立不等于敌对应用安全隔离。

结论范围：`per-invocation-context`。原始动作、状态和功能证据缺失时不填 PASS。

## SH06 — 短命启动器被结束时租约仍由真实工作进程持有

优先级：P0；模式：`live_debian`；初始状态：`NOT_RUN`。
前置：SH05。

步骤：

1. 启动短时 CLI 和其受管工作进程，保存 PID/start 与客体标记。
2. 仅对本轮确证启动器发一次 SIGKILL；继续核对客体执行与最终退出。

观察点与通过条件：

- 启动器消失不能被当作客体结束；租约继续有效至真实执行结束。
- 其他应用不受影响，最后清理同一 CID；未知状态不猜成功。

结论范围：`supervisor-lifetime`。原始动作、状态和功能证据缺失时不填 PASS。

## SH07 — 手动启动环境的停止所有权

优先级：P0；模式：`live_debian`；初始状态：`NOT_RUN`。
前置：SH04。

步骤：

1. 明确手动启动选定测试 CID，再用正式入口运行并关闭 A/B。
2. 核对最后租约离开后环境状态，测试结束由自身显式停止。

观察点与通过条件：

- 产品不擅自停止原本已运行的环境。
- CID 不变；测试收尾回到原停止状态，不据此否定 SH04 的自动停止契约。

结论范围：`manual-start-ownership`。原始动作、状态和功能证据缺失时不填 PASS。

## SH08 — 启动失败、错误身份和未知状态的局部回收

优先级：P0；模式：`offline_and_live_debian`；初始状态：`NOT_RUN`。
前置：SH02。

步骤：

1. 在 B 存活时启动无效 argv 的测试 A，验证只回收 A。
2. CID/generation/标签替换、PID 复用和未知客体存活使用 fake runtime 注入，不修改真实所有权。

观察点与通过条件：

- B 的执行和共享实例保持正常，失败结果非零且归因明确。
- 未知时保留阻断/租约证据，不另建容器、删记录或杀整个共享实例凑通过。

结论范围：`failure-isolation`。原始动作、状态和功能证据缺失时不填 PASS。

## SH09 — 同容器包事务锁竞争与应用共存

优先级：P0；模式：`offline_and_live_debian`；初始状态：`NOT_RUN`。
前置：DEP01, SH08。

步骤：

1. 使用两个本轮独有的小包提交受控锁竞争，实际只允许一个原生写事务。
2. 同时保留一个不依赖被修改测试库的轻量 CLI B，观察其执行和 CID。

观察点与通过条件：

- 第二请求按实际契约等待或明确报忙，不并行写包数据库；没有新环境。
- B 不被误杀；原生写入时间区间不重叠，最终事务与包库一致。
- 不与其他发行版包管理或真实 GUI 依赖变更并行。

结论范围：`shared-package-lock`。原始动作、状态和功能证据缺失时不填 PASS。

## SH10 — 真实 Writer 与 CLI 共用实例并有界重复

优先级：P1；模式：`live_debian_gui`；初始状态：`NOT_RUN`。
前置：DEP11, SH04。

步骤：

1. 使用已正式安装并完成能力预检的 Writer 与一个 CLI，私有显示在首次受管启动后接入，不能手动启动伪造组所有权。
2. 编辑本轮文档标记、保存、关闭、重开核对；关闭其中一个时核对另一个。
3. 最多 5 轮、累计最多 10 分钟，达到定义内验收就停止。

观察点与通过条件：

- 所有轮次使用同一 CID，窗口与文件内容双重验证，原始输入不变。
- 关闭一个不影响另一个；自动停止证据只有在确实由组启动时成立。
- 云辅助禁用、账号未验或清理强杀不能被标成完整套件/正常关闭通过。

结论范围：`real-gui-shared-workflow`。原始动作、状态和功能证据缺失时不填 PASS。

## DEP01 — 原生声明依赖经正式入口安装

优先级：P0；模式：`live_debian`；初始状态：`NOT_RUN`。
前置：PRE02, PRE03, PRE04, PRE05。

步骤：

1. 安装一个带准确 Depends 的本轮小包，使用正式 install/pkg install。
2. 核对原生依赖解析、最终事务与包清单，并以普通用户运行功能 oracle。

观察点与通过条件：

- 包声明依赖由目标原生包管理器完成，最终 phase/verification 与实际输出一致。
- 不触碰宿主包库或非目标容器；没有人工补装充当自动结果。

结论范围：`declared-dependencies`。原始动作、状态和功能证据缺失时不填 PASS。

## DEP02 — 未声明 ELF 运行库真实自动补装

优先级：P0；模式：`live_debian`；初始状态：`NOT_RUN`。
前置：DEP01。

步骤：

1. 预先证明 fixture 的 DT_NEEDED 所需库不存在、可信仓库提供者唯一且未被基线应用使用。
2. 安装遗漏该 Depends 的 fixture，经默认自动补库入口执行，不先手动安装提供者。
3. 检查扫描、候选、选中包、重扫、实际执行和前后清单。

观察点与通过条件：

- 缺失条件有独立证据；自动补装后 phase=completed、verification.ok=true、unresolved 为空。
- 实际功能输出正确；库不可读、选择歧义或缺失条件不成立不得记为真实修复通过。

结论范围：`undeclared-elf-runtime`。原始动作、状态和功能证据缺失时不填 PASS。

## DEP03 — 只在插件中出现的 DT_NEEDED

优先级：P0；模式：`live_debian`；初始状态：`NOT_RUN`。
前置：DEP02。

步骤：

1. 准备主程序本身不依赖目标库、包内插件 .so 依赖另一个真实缺失库的 fixture。
2. 通过正式安装扫描插件，再实际 dlopen 插件并执行已知函数。

观察点与通过条件：

- 扫描范围包含该插件而不仅是主可执行文件。
- 自动修复和实际插件调用均成功；不能复用已安装库导致误报覆盖了缺失场景。

结论范围：`plugin-needed`。原始动作、状态和功能证据缺失时不填 PASS。

## DEP04 — 多个应用复用同一提供者

优先级：P0；模式：`live_debian`；初始状态：`NOT_RUN`。
前置：DEP02。

步骤：

1. 在 A 已补入提供者后安装需要同一库的 B。
2. 比较提供者版本/安装动作和 A/B 实际加载路径、库文件标识。

观察点与通过条件：

- 共享同一个目标环境包版本，不因 B 新建容器或重复安装无关提供者。
- A/B 都实际工作；相同文件/包名不自动等同 ABI 或物理 RAM 共享证明。

结论范围：`shared-dependency-provider`。原始动作、状态和功能证据缺失时不填 PASS。

## DEP05 — 重复 repair 的无新增依赖结果

优先级：P0；模式：`live_debian`；初始状态：`NOT_RUN`。
前置：DEP04。

步骤：

1. 在状态不变的同一 CID 连续执行两次正式 repair-deps。
2. 核对事务 installed 列表、包版本和原生动作差异。

观察点与通过条件：

- 无需补库时 installed 为空，无新增无关包和无新增环境。
- 查询/索引校验允许发生并记录；静态检查完成不变成 GUI/运行加载全通过。

结论范围：`repair-idempotence`。原始动作、状态和功能证据缺失时不填 PASS。

## DEP06 — 移除应用 A 不破坏应用 B 的共享库

优先级：P0；模式：`live_debian`；初始状态：`NOT_RUN`。
前置：DEP05。

步骤：

1. 经正式 remove 仅删除本轮 fixture A，不使用全局 autoremove。
2. 确认 B 仍在清单，重开并执行同一函数，核对公用提供者。

观察点与通过条件：

- A 已移除，B 和其需要的库仍可实际使用，CID 不变。
- 不根据不完整引用计数删除公用库；未知引用保留并报告，不伪称依赖清理已实现。

结论范围：`shared-dependency-retention`。原始动作、状态和功能证据缺失时不填 PASS。

## DEP07 — 共享提供者兼容升级及不兼容版本边界

优先级：P1；模式：`offline_then_live_fixture_if_ready`；初始状态：`NOT_RUN`。
前置：DEP06。

步骤：

1. 先离线验证兼容/不兼容提供者变更，再准备同 SONAME 兼容升级的独有签名 fixture 包。
2. 在所有 GUI 关闭、备份就绪时升级 fixture，重开 A/B 核对旧 API。
3. 不兼容主版本变更只在受控 fixture 中测试拒绝/并存/明确能力缺口，不更换真实系统库。

观察点与通过条件：

- 兼容升级后两应用仍可运行且不造新环境；加载库版本和结果有证据。
- 无法兼容不能假成功或用软链接替代 ABI；本用例不宣称已实现通用依赖版本求解器。
- fixture 仓库/签名/预算不足即 BLOCKED，不伪装官方仓库。

结论范围：`shared-dependency-upgrade`。原始动作、状态和功能证据缺失时不填 PASS。

## DEP08 — 运行时构造库名与精确 library 提示

优先级：P0；模式：`offline_and_live_debian_if_missing`；初始状态：`NOT_RUN`。
前置：DEP01。

步骤：

1. 用仅在运行时构造 dlopen 名称的 fixture，记录静态检查与实际加载不同的结果。
2. 在独立证明真实缺失和唯一提供者后，经 repair-deps --library 精确 SONAME 修复，再实际调用。

观察点与通过条件：

- 静态 no-missing 不能标为运行加载已验收，runtime_loading 边界如实保留。
- 显式 --library 路径可完整验收；如果库原本存在，只能算 no-op/离线契约覆盖。

结论范围：`runtime-hinted-loading`。原始动作、状态和功能证据缺失时不填 PASS。

## DEP09 — 无提供者或多提供者时不猜包

优先级：P0；模式：`offline`；初始状态：`NOT_RUN`。
前置：PRE02。

步骤：

1. 用受控查询响应分别返回零候选、多候选和部分错误输出。
2. 检查错误、事务、安装动作和尝试次数。

观察点与通过条件：

- 明确失败并保留候选/原因，不猜选、不乱装、不创建环境。
- 这是离线契约证据，不称为真实仓库闭环；可选实机复核另受签名 fixture 条件限制。

结论范围：`provider-ambiguity`。原始动作、状态和功能证据缺失时不填 PASS。

## DEP10 — 索引错误、安装失败和补装无效的有界退出

优先级：P0；模式：`offline`；初始状态：`NOT_RUN`。
前置：DEP09。

步骤：

1. 分别注入仓库/索引失败、安装非零、安装后库仍不可用、递归依赖不收敛。
2. 核对最多 3 轮扫描的当前边界、失败阶段与可恢复记录。

观察点与通过条件：

- 不会无限联网/循环补库或继续启动 GUI，不留虚假的 completed。
- native-command/dependency-repair/verification 与 recovery 状态明确区分，未知写入状态按恢复契约阻断。

结论范围：`repair-bounded-failure`。原始动作、状态和功能证据缺失时不填 PASS。

## DEP11 — GIO 等外部命令和 GUI 能力缺口

优先级：P0；模式：`offline_and_live_debian`；初始状态：`NOT_RUN`。
前置：DEP01。

步骤：

1. 检查实际 gio、字体/MIME、Qt 后端和必需桌面服务；将 WPS 脚本优先 gio 的路径纳入能力清单。
2. 缺失变体先离线注入；实机天然缺失才验证准备/修复，已有公用能力不得卸载以制造报错。
3. 补齐能力后实际验证 gio/MIME 的预期作用及私有文档界面；手工准备与自动修复分别记账。

观察点与通过条件：

- 缺脚本命令不能被 ELF 检查成功掩盖，更不能据此标完整应用 ready。
- 若尚无自动命令修复，记录 feature_gap；识别缺口不等于自动修复通过。
- 不接受改变宿主默认应用；不追装旧 gvfs 工具替代已经支持的现代分支。

结论范围：`external-command-and-gui-capabilities`。原始动作、状态和功能证据缺失时不填 PASS。

## DEP12 — 实际搜索路径、符号版本、架构与厂商配置的分类

优先级：P0；模式：`offline_then_private_fixture`；初始状态：`NOT_RUN`。
前置：PRE04。

步骤：

1. 离线准备同 SONAME 不同私有路径、缺符号版本、非 x86_64 ELF 和非法配置样本。
2. 必要时以普通用户在本轮私有 fixture 中验证加载路径和实际函数，禁止改宿主或公用系统库。
3. 对照 WPS 已知非法 GPU JSON 和辅助进程故障，检查报告是否误归类为可补库。

观察点与通过条件：

- bundled_candidates/静态满足不冒充实际 RPATH/ABI 成功；未支持架构明确拒绝。
- 厂商配置/内部异常与缺库、缺外部命令分开，保留功能影响和 feature_gap。
- 不会通过软链接库版本、全局 LD_LIBRARY_PATH 或吞 stderr 凑通过。

结论范围：`abi-loader-and-error-classification`。原始动作、状态和功能证据缺失时不填 PASS。

## DEP13 — 补库包事务中断与同一实例恢复

优先级：P1；模式：`offline_then_live_debian_fixture`；初始状态：`NOT_RUN`。
前置：DEP06, SH09。

步骤：

1. 先离线确认恢复契约；停止所有 GUI，在本轮独有小包维护步骤中只注入一次可控中断。
2. 保存客体 PID/start/CID、事务和包清单；先执行正式 pkg recover，再运行 B 和后续合法事务。

观察点与通过条件：

- 未知状态阻断继续改包；恢复重读原生数据库，绝不换容器/删事务逃避半安装状态。
- 同一 CID、B 功能和共享依赖得到复核；无法安全恢复即停止并保留备份。

结论范围：`package-recovery-in-shared-instance`。原始动作、状态和功能证据缺失时不填 PASS。

## AR01 — Arch 同实例轻量应用复用

优先级：P1；模式：`live_arch`；初始状态：`NOT_RUN`。
前置：AR03, SH04。

步骤：

1. 确认 Debian 阶段已停止，使用选定 Arch 原管理根登记两个 CLI。
2. 执行一次冷组、热开 B、关 A 保留 B、关最后一个的过程。

观察点与通过条件：

- 固定同一 Arch CID，无新建；同时间只运行这一台测试容器。
- 租约和手动/受管启动所有权契约与 Debian 主线一致。

结论范围：`arch-shared-runtime`。原始动作、状态和功能证据缺失时不填 PASS。

## AR02 — Arch 依赖与共享提供者最小复核

优先级：P1；模式：`live_arch`；初始状态：`NOT_RUN`。
前置：AR01, DEP02, DEP03, DEP06。

步骤：

1. 使用 Arch 原生小包和真实唯一缺失提供者，复核插件补库、A/B 共享、重复 repair 与移除 A 保留 B。
2. 完整记录 pacman 文件查询、最终事务和真实加载结果，不能拿 Debian 包名或未完成事务当通过。

观察点与通过条件：

- 唯一可信候选且重新检查与运行通过，CID 保持不变。
- 当前提供者适配只接受其实际支持的仓库记录，不能伪装 core/extra 仓库名称扩展覆盖。

结论范围：`arch-dependency-recheck`。原始动作、状态和功能证据缺失时不填 PASS。

## AR03 — Arch keyring、整体升级和交互预算准入

优先级：P1；模式：`readonly_then_live_arch_preparation`；初始状态：`NOT_RUN`。
前置：PRE02, PRE03, PRE04, PRE05, DEP06。

步骤：

1. 只读/受支持预估方式核对 keyring、仓库和完整 -Syu 下载/空间预算；大于本阶段 200 MiB 预算则延期。
2. 条件满足才通过原生入口准备一次；交互模式保留确认、拒绝、退出码和终端状态。

观察点与通过条件：

- 不关闭签名、不使用部分升级、不自动下载超预算内容或新建更换镜像实例。
- Decline/网络/签名失败是明确结果，不能改成后台无限等待或套用 -y 冒充真实交互。

结论范围：`arch-native-preparation`。原始动作、状态和功能证据缺失时不填 PASS。

## END01 — 固定池、数据、服务和资源收尾

优先级：P0；模式：`readonly_and_owned_cleanup`；初始状态：`NOT_RUN`。
前置：无；END01 为 finally 必执行。

步骤：

1. 无论前置成功与否，只结束本轮确证应用、控制器、显示、转发和本轮启动的选定环境。
2. 核对全局 CID 集合、非目标事件/状态、宿主安装/账号/输入哈希、包/注册/仓库临时项、Git 索引。
3. 保留中止锁、失败样本、NOT_RUN 项和公共能力新增清单，不删除历史环境或未知 IPC。

观察点与通过条件：

- 新增容器 0，选定实例回到原状态；非目标无本轮 create/start/restart/exec 或非授权修改。
- 本轮进程/监听退出，用户主动改变非目标状态时暂停并标外部干扰，不擅自关闭用户应用。
- 恢复不完整必须报告 BLOCKED/未收尾，不能用 reset/prune 隐藏现场。

结论范围：`cleanup-and-preservation`。原始动作、状态和功能证据缺失时不填 PASS。
