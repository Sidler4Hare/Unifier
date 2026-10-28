# Unifier v2 F：本机压力与性能验证报告

日期：2026-10-26（Asia/Shanghai）。结论：**本机测量矩阵与保全核验已完成；文件投影一致性关口未通过，不能宣布 F 全阶段通过。**

本轮未修改 A–E 产品源码、测试或日常部署。沿用 dsh Command Code / deepseek-v4.1-flash 编写与复核测量工具，由主控执行真实负载并独立复算。机器可读结论见 [acceptance-result.json](acceptance-result.json)，独立复核见 [primary-verification.json](primary-verification.json)。

## 已完成范围

- Fedora N=1/2/4/8，各 3 波；三发行版混合各 2 个会话，共 2 波。每发行版始终共享一个环境。
- 三发行版各 30 次停止环境启动执行、30 次已运行环境执行；合计 180 个计时样本。包括并发负载，正式会话记录合计 237 个，全部 exited / exit 0 / guest gone / lease 空。
- Podman 原始 start 事件独立核对：Fedora 45、Debian 33、Arch 33，合计 111；窗口内没有 create 事件。
- 每发行版宿主直读、容器 bind、真实产品 FUSE 各 30 个负载样本，合计 270；45 次新视图获取和撤销。读取内容、大小和哈希经实际负载校验。
- Debian 停止环境投影持续测试 180.024 秒：2703 轮读取、181 次宿主原子替换。该持续轮次只覆盖 Debian fixture。

## 执行与缓存条件

Fedora 44 KDE Wayland，内核 7.2.11-200.fc44.x86_64，rootless Podman 5.8.9，8 逻辑 CPU，约 7.43 GiB RAM。日常桌面、powersave governor、未锁频，未清页缓存。三套客体使用固定镜像、keep-id、相同绝对路径 bind，以及 fixture 冻结的源码与配置。

测量顺序固定为宿主→容器 bind→产品 FUSE；客体中的 shell/coreutils 并未统一版本。以下结果用于描述这台机器上的实际链路，不是随机化因果比较、内核冷缓存、容量上限或性能 SLO。first-observed 与 warm 的原始分组保留于投影 summary。旧 SFTP 原型数据不与本轮产品 FUSE 数据拼接。

## 项目命令耗时

完整公共 CLI 命令执行链路，包含 shell、4 KiB 输出、身份和哈希核验及会话退出；不是 GUI 首帧或单独 Podman 启动时间。单位毫秒。

| 客体 | 条件 | n | min | p50 | p95 | max |
|---|---|---:|---:|---:|---:|---:|
| debian13 | cold | 30 | 2790.5 | 2863.0 | 2961.5 | 2961.6 |
| debian13 | warm | 30 | 2104.6 | 2178.0 | 2341.5 | 2472.5 |
| fedora44 | cold | 30 | 2825.8 | 2916.8 | 3091.0 | 3299.4 |
| fedora44 | warm | 30 | 2139.4 | 2195.9 | 2308.8 | 2469.3 |
| arch | cold | 30 | 2902.8 | 2985.7 | 3097.4 | 3274.9 |
| arch | warm | 30 | 2242.1 | 2304.1 | 2556.4 | 4346.4 |

解析/授权 prepare 与公共 CLI plan 分开保留于 [会话原始汇总](sessions-formal-1/summary.json)；这些预检没有启动停止中的 fixture。计时区间尽量暂停资源采样；暂停不是同步屏障，不主张完全排除观测开销。

## 文件路径负载

每个样本执行 2,000 个 4 KiB 文件的 stat、全部小文件读取并计算聚合哈希，以及 16 MiB 顺序读取并计算哈希。表内为 p50 / p95 毫秒，完整 count/min/p50/p95/max 见独立复核 JSON。

| 客体 | 路径 | n | 2,000 stat | 2,000 read+hash | 16 MiB read+hash |
|---|---|---:|---:|---:|---:|
| debian13 | host-direct | 30 | 11.1 / 16.4 | 43.5 / 62.6 | 29.4 / 40.7 |
| debian13 | container-bind | 30 | 11.7 / 14.7 | 42.0 / 51.0 | 28.6 / 32.5 |
| debian13 | fuse-projection | 30 | 448.9 / 531.0 | 918.1 / 981.5 | 749.9 / 812.0 |
| fedora44 | host-direct | 30 | 10.7 / 12.2 | 40.4 / 55.9 | 28.9 / 33.9 |
| fedora44 | container-bind | 30 | 13.1 / 15.0 | 44.0 / 57.6 | 28.9 / 33.9 |
| fedora44 | fuse-projection | 30 | 456.7 / 518.9 | 949.5 / 1097.4 | 768.4 / 883.3 |
| arch | host-direct | 30 | 10.8 / 12.1 | 41.9 / 52.9 | 29.0 / 33.4 |
| arch | container-bind | 30 | 12.8 / 14.0 | 23.4 / 32.2 | 29.9 / 34.7 |
| arch | fuse-projection | 30 | 449.7 / 473.4 | 934.3 / 1154.8 | 738.1 / 844.1 |

45 次视图 open 的 p50 / p95 为 547.4 / 573.1 ms；revoke 为 143.2 / 200.9 ms。45 次 worker/broker/launcher、套接字与挂载清理检查均通过。

本轮 bind 与宿主直读在相近量级；产品 FUSE 各阶段 p50 相对宿主约 21–43 倍。普通共享项目继续使用直接 bind。当前投影性能不足以据此承诺大型 IDE、索引或构建树体验。

## 文件语义关口

相对链接经过新的三发行版真实视图补验：readlink 返回投影内绝对路径，readlink-raw 保留 small/f0000，实际跟随链接读取的内容正确。这符合 C 阶段 projected-link 契约。绝对链接、只读 mmap、写打开拒绝及视图清理同样通过。

但下列实际结果阻止通用文件一致性验收：

1. **打开对象身份不稳定。** 三发行版均出现：先打开、尚未读的投影 FD 在宿主原子替换后读到新文件；同一操作的原生 FD 仍读取旧 inode。预先读过的投影 FD 暂时保留旧缓存，不能据此证明打开对象语义正确。
2. **大小与内容不一致。** 三发行版均把 32 字节文件替换成 128 字节新内容后，立即读取只返回新内容前 32 字节，既非完整旧版本也非完整新版本；等待 1.3 秒后读取完整新版本。
3. **缺少外部修改通知。** 宿主控制 watcher 实际收到修改/rename 事件，三个投影 watcher 均为 NO_EVENT；持续测试同样如此。掩码已排除读操作事件。
4. **路径缓存上限未证明。** 先前真实投影的 12,000 个唯一路径观察中 worker PSS 约从 12.5 MiB 增至 17.4 MiB，短时空闲未回落、FD 为 7。它证明该负载下增长，未证明长期上限或泄漏。

精确原始观测见 [semantic-recheck-1.json](semantic-recheck-1.json) 与 [primary-cache-attempt1.json](primary-cache-attempt1.json)。上述问题未在本轮修改产品源码修复。

## 持续负载与资源覆盖

最终持续轮次 180.024 秒，271 次 1 MiB 顺序分块校验。current.bin 共读取 2703 次，其中 2694 次与当时最新 writer 记录相符，9 次为旧值或竞争窗口；该比率不是强一致性通过率。

- sessions-formal-1：407 个资源样本；采样间隔 p50 / max = 1.000 / 4.468 秒。会话计时段主动暂停采样，间隔不能当持续覆盖。
- soak-formal-2：181 个资源样本；采样间隔 p50 / max = 1.000 / 1.000 秒。会话计时段主动暂停采样，间隔不能当持续覆盖。

| 会话资源系列 | 观测 running 样本 | cgroup 可读 | guest PSS 完整 | guest PSS 部分 |
|---|---:|---:|---:|---:|
| debian13 | 42 | 42 | 42 | 0 |
| fedora44 | 119 | 119 | 101 | 18 |
| arch | 41 | 41 | 40 | 1 |

停止环境不存在运行中 guest cgroup/PSS，保留 UNKNOWN；持续轮次运行的是宿主 FUSE/存储代理，不把停止 guest 记为零内存。资源摘要的 FD/thread/RSS 聚合受 PSS 可读集合限制，不能当完整进程总量。cgroup memory.current 与 PSS 分别报告、不相加。完整指标、错误与覆盖率见两个资源 summary/jsonl。运行状态有约 5 秒 inspect 缓存，边界样本也有观测延迟。

## 失败、补验与证据保留

- sessions-pilot1：短任务导致 lease 观测竞态；sessions-pilot2：统计汇总把 dict 当数值。均保留，正式矩阵使用就绪屏障与修正统计。
- projection-formal-1：主控目录大小检查意外遍历活动投影，样本受干扰后中止。中止时子负载短暂占用视图，首次撤销被 busy 保护拒绝；负载结束后经产品接口重试撤销成功，见 primary-recovery.json。整轮排除于性能结论。
- projection-formal-2：270 个性能样本与 45 次清理均完成；原 summary 仍保留 relative_link_mapping=false。原因是探针错误地要求相对链接文本不变；根据既有 C 设计与实现修正探针，再独立补验三套新视图。原失败 summary 没有改写。
- soak-formal-1：记录到约 101 秒采样空档，只有 88 次替换/采样，未达连续覆盖；原因未定位，不算通过。最终采用新目录 soak-formal-2 连续重跑，原失败保留。
- 宿主 watcher 路径错误、修改事件掩码和空采样检查已修正；本轮工具 selftest 9 组通过。产品 359 项基线为 355 通过、4 条件跳过，原日志与当前 107 个源码/测试/工具/配置文件哈希匹配；本轮没有重复运行产品回归，也不以此覆盖新发现的文件语义失败。

工具协作记录见 [agents/continue-review-handoff.md](agents/continue-review-handoff.md)。测量时的原版投影探针以 agents/probe_projection-measured.py 和原 SHA-256 保留，修正后的版本与摘要另存。

## 清理与保全

仅移除 manifest 中本轮三个精确 CID。独立核对：原有 27 个容器的 CID/名称/状态不变；23 个主状态文件、14300 个旧证据文件和 107 个源码/测试/工具/配置文件哈希无变化；卷清单一致。

宿主及 rootless mount namespace 中没有本轮视图或 storage layer 挂载，捕获的 worker/broker/launcher 与 session worker 已退出，租约/视图注册为空，测试环境记录已移除。见 [primary-preservation.json](primary-preservation.json)、[primary-postconditions.json](primary-postconditions.json) 和 [cleanup-final/summary.json](cleanup-final/summary.json)。

## 下一轮入口

本机优先修复打开对象身份与大小/内容一致性，再补外部修改通知和路径缓存回收，随后用本轮案例与性能矩阵复验。文件锁/SQLite、Git/IDE 大树与更长持续运行也仍需独立实际验收。保留原始输入和失败案例，修复使用新 fixture 快照与新证据目录。

跨宿主待办仍为 PENDING_EXTERNAL_HOST：Debian/Ubuntu 宿主、Arch 宿主、第二类 Wayland 桌面、独立硬件性能复测、真实断电与独立磁盘故障。详见设计/Unifier-v2-第六阶段本机验证.md。

分支 codex/unifier-v2-control；未提交、未推送。原 fixture CID 已清理，不能直接重跑旧 manifest；需通过 prepare_fixtures.py 指定全新的 output/project-dir/workspaces 后重测。
