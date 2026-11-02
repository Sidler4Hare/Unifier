# 只读投影 FD 耗尽分析（2026-10-29）

分析基线：Git HEAD bb38f7a，产品源码未修改。本轮新增本目录的 probe.py、result.json 和报告。未启动或修改日常容器，未挂载 FUSE，未调整其他进程的资源上限。

## 结论

优先采用“轻量目录清单 + 真正 lookup 对象继续锚定 + 统一 FD 预算和控制余量”。当前主要放大点是 OPENDIR 为所有子条目长期持有 O_PATH anchor，而不是只有用户实际打开的文件才占 FD。资源释放和失败回滚在本轮样本中正常，不能将这种设计性的资源保留笼统称为 close 泄漏。

该方案解决纯目录浏览的线性 FD 放大；大规模 stat/lookup 仍会留下内核引用，必须单独治理，不能宣称目录改造后 FD 问题全部解决。

## 当前调用链

- projection.py:913 起 opendir 打开目录 FD，然后调用 _snapshot。
- _snapshot:818 对每个名字 _lookup(count_ref=False)，833 增加 internal 引用。
- _lookup 为子对象建立 O_PATH FD；_allocate_node 按 provider/mount/dev/ino 去重。同一个对象的硬链接或重复目录流不会各自新增 anchor，但增加内部引用；不同对象/目录仍会线性累积。
- release(dir) 释放条目 internal 引用；_collect 仅在 nlookup、opens、internal 均为零时关闭锚点。根节点单独常驻。
- LOOKUP 为节点增加 nlookup，用户关闭文件只减少 opens。FORGET/BATCH_FORGET 才减少 nlookup；TTL 0 并不等价于马上收到 FORGET。
- 普通 READDIR 返回名字、类型、inode 和 cookie，不向内核授予 lookup 引用。当前代码给清单条目建立长期对象引用，是实现选择，不是该协议的要求。

近似资源模型：基础 FD + provider 根 FD + 活跃真实节点 anchor + 文件/目录 handle FD + 瞬时扫描/身份检查/控制 FD。现有 handles=1024 只约束 handle 表，不包含 nodes=65536 对应的锚点。目录 entries=65536、bytes=8MiB 是另外的账面预算。

## 本轮隔离复现

运行：`python3 evidence/projection-fd-analysis-2026-10-29/probe.py`。

脚本在独立进程内把 soft RLIMIT_NOFILE 降至 256，结束时恢复；原继承 soft/hard 均为 524288。这不是生产 worker 的限额测量，不能将 256 当成用户现场的触发阈值。使用当前真实对象层和真实 OS FD；没有经过内核 FUSE 请求及其缓存回收时序。

| 场景 | 结果 |
|---|---|
| 打开含 100 个不同文件的目录 | FD 6→107；nodes 1→101，只有 1 个 dir handle；关闭后 FD=6 |
| 打开含 300 个文件的目录 | EMFILE(24)；回滚后 FD=6、nodes=1、handles=0 |
| 逐个 lookup，保留一个已打开文件 | 第 250 个成功 lookup 后，后续请求 EMFILE；旧 handle 仍读出 old |
| 关闭唯一文件 handle | nodes 仍为 251、lookup refs=250，尚未释放锚点 |
| 对所有成功 lookup 发出 FORGET | FD 回到 6、nodes=1；新 lookup 恢复 |

耗尽时 /proc/self/fd 的枚举自身失败，result.json 用 -1 表示不可测，不是负 FD 数。其余 FD 计数采用相同枚举方式，有观察操作本身的瞬时开销。

相关现有回归：`python3 -m unittest discover -s tests -p 'test_projection*.py' -q`，47 项通过。此次未运行全量、真实挂载或 GUI 验收。

历史 projection-objects-2026-10-26/report.md 已明确大目录锚点限制并验证耗尽恢复；该验证证明资源错误/恢复行为，不等于大目录容量足够。

## 修复方案比较

| 方案 | 收益 | 代价或边界 | 建议 |
|---|---|---|---|
| 提高 worker NOFILE | 实现小，增加容量 | 不减少每节点资源；本次分析进程继承上限已很高，需先量真实 worker | 仅按需部署缓解 |
| 降低目录/节点预算 | 防止用光进程 FD | 更早拒绝目录，不能改善可浏览容量 | 作为保护措施 |
| 目录清单不保留子条目 anchor | 纯浏览时持久 FD 从随条目增长变为随打开目录增长 | 需保留 cookie、inode、provider、安全和错误语义；仍有 O(N) 清单内存 | 首选结构修复 |
| LRU 强关 lookup 活跃节点 anchor | 表面降低 FD | 丢失旧对象，rename/unlink 后按路径重开可能错读，新旧 inode 复用风险 | 不采用 |
| name_to_handle_at/open_by_handle_at | 某些后端可以按对象标识重开 | 权限及文件系统支持限制，删除可 ESTALE；不是原 FD 语义的通用替代 | 后续按 provider 探测 |
| 真正不可变快照后端 | 便于稳定按路径重新定位 | 多卷一致性、空间与生命周期是另一项架构工作；停止容器不冻结 bind 数据 | 独立能力 |
| 多进程或更换语言 | 分散进程限额或降低部分开销 | 不消除总锚点数，增加协议与生命周期复杂性 | 不作为本问题首修 |

## 推荐实施细节

1. 分离目录记录和活跃对象节点。目录记录仅保存 name、kind、外部 inode、cookie 及必要的 provider 身份，不持有 Node/internal ref。最保守迁移方式是每次仅短暂获取一个条目的 anchor/fstat，计算同样的外部 inode 后立即释放临时引用；后续再评估安全的相对 stat 优化。不能直接使用底层 d_ino 作为投影 inode。
2. 保留每个打开目录自身的 FD，使目录改名后仍围绕原目录对象工作；保持原有名称清单和顺序，继续使用固定清单索引作为 cookie。目录流看到已删除条目的历史名字是允许的；后续 LOOKUP 仍识别当时的名称绑定。这不再承诺每个历史清单条目的底层对象一直存活，也不应把清单称作文件内容快照。
3. 已存在的 lookup/open 节点保持现有引用和 FD 语义。不可将 nlookup 清零或驱逐内核仍引用的节点。当前无引用节点已立即回收，单加 LRU 的收益很小。
4. 外部 inode 映射继续复用 view/provider/mount/dev/ino 的规则；普通硬链接同号、跨 provider 分离、关闭重开不变。清单不再持有节点后，必须重新设计清单与活跃节点之间的碰撞检测生命周期，避免只依赖当前 _inode_owners 表而弱化既有碰撞拒绝契约。并发删除后的 inode 复用要按 live 目录观察语义定义，不能假称获得永久对象身份。
5. 建立共享 FD 预算。读取实际 worker RLIMIT_NOFILE，把 provider、anchor、handle 和临时操作需求纳入准入，保留控制 socket accept、状态/日志写入、provider 检查等余量。预留值根据最坏调用链及并发数量验证，不能凭经验声称固定数足够。超限提前拒绝新增对象，保持旧 handle 读取、RELEASE、FORGET 和正常清理可执行。外部变更限额及系统 ENFILE 仍须正确处理。
6. 增加 anchor_fds、lookup-only nodes、snapshot refs、当前/峰值 FD、预算拒绝次数、实际 soft/hard limit 等观测。纯 readdir、stat 扫描和实际 open 分开报告。
7. 目录字节/条目预算仍保留。首次改造不要求顺便取消排序、实现流式合并或 READDIRPLUS；这些会引入额外 cookie/引用契约。超大清单需要另做有界内存/磁盘溢出设计。

## 必须完成的后续验收

- 低 worker NOFILE 下，300/3000/10000 条目的纯 readdir 成功，目录关闭前的 FD 增量不随条目数线性增长；内存仍受声明预算限制。
- 单独测 ls -l/stat/递归索引及其真实内核 FORGET 时序，公开剩余 lookup 压力，禁止把纯浏览改进当成索引场景通过。
- 多个目录流、同目录重开、硬链接、注入挂载/缺失祖先、不可访问项、provider 失效、碰撞回滚及资源错误保持原契约。
- 分页中 rename/unlink/replace，不漏掉或重复无关项；旧打开文件在 replace/unlink 后保持原对象，GETATTR(fh)/mmap 不回退。
- 压力下控制状态查询可响应；关闭/FORGET 后自动恢复，最终 FD/引用/目录账面计数归位；失败信息保留真实 errno。
- 在隔离真实 FUSE 挂载、完整 broker 链路和实际文件管理器分别复测。生产 worker 实际限额、峰值和用户原始现场仍待采样。

参考：[libfuse lookup/forget/readdir 合同](https://libfuse.github.io/doxygen/structfuse__lowlevel__ops.html)、[open 对象与路径语义](https://man7.org/linux/man-pages/man2/open.2.html)、[open_by_handle_at 权限与 ESTALE](https://man7.org/linux/man-pages/man2/open_by_handle_at.2.html)。
