# 2026-11-01 本地 Qwen 编码与继续修复测试

本轮在工作树完成修复与验证，离线检查 **633 项：628 通过、5 条件跳过、0 失败**。未构建新发布包或更新日常部署，现有冻结 v0.0.2 保留。完整 GUI、媒体、Agent 桥、Fedora 客体仓库与长压仍未验收。

## 本地模型实际产出

用户澄清后停止了最初误用的三个内置子代理，后续编码通过 dsh 的 `Local qwen server` / `swift-1.5-iq3_s` / `max` 完成。原提供商配置未更改；私有调用覆盖只存于 `.lab/agents`，不放进报告或补丁。临时覆盖初次因 Python YAML 将 `off` 转为布尔键导致适配器未激活，修正序列化后成功推理，原失败回执保留；这次启动错误不计作模型编码结果。

1. **包文件列表路径损坏。** Qwen 修改 `src/unifier/packages.py`、`tests/test_app_entries.py`：仅以 LF 分行，保留路径尾部空格、Unicode 分隔字符和 shell 字面字符；路径中的 ASCII C0/DEL 明确拒绝。主控从修前独立快照回放新增测试，得到 17 个预期失败；新代码 37 项相关测试全部通过。真实 Debian `base-files` 100 条和 Arch `filesystem` 127 条文件记录查询通过，查询后客体恢复停止。见 [回执](qwen-task1-receipt.json)、[独立复核](qwen-task1-validation.json)、[补丁](qwen-task1.patch)。文件名本身含 LF 仍无法由原生命令的 LF 列表无损表达，未宣称解决。
2. **默认实机回执随临时根删除。** Qwen 修改 `tests/podman_test_support.py`、`tests/test_integration_policy.py`：删除临时根前复制默认回执到 `artifacts/integration-runs/<唯一根名>/`；目标已存在或保全失败时报错并保留原根；显式外部输出不迁移。主控独立回放旧 helper 得到 2 个预期失败，新 helper 6 项通过。见 [回执](qwen-task2-receipt.json)、[独立复核](qwen-task2-validation.json)、[补丁](qwen-task2.patch)。

两个任务都有真实文件编辑、修前失败及修后测试；模型完成声明未直接作为验收。两个任务各使用 17、14 个 dsh 步骤，`max` 下前置探索较多；本轮支持“小范围修复可交付”的结论，不能据此推断大型任务表现。模型原文在 `qwen-task1-self-report.md`、`qwen-task2-self-report.md`，其中建议仍属于模型自述。

## 此前保留的修复与主控工作

用户澄清前，内置代理已落盘的修改保留并独立复核，未归为本地 Qwen 产出：

- `service.py` 与恢复回归：包变更入口、原生命令各步、清单与本地包暂存核对记录 CID，并让真实调用固定 CID；停止替代容器不能证明旧执行已结束。三条替代 CID 反例通过。真实 Debian 无变更补库事务通过、清单固定 CID 读取，篡改测试记录 CID 后在任何客体执行前拒绝；见 [实机回执](real-package-identity.json)。该实机探针未做真实同名容器替换竞争，也没有执行安装/升级。
- `dependencies.py` 与补库回归：每轮重扫原包及已补装提供者，拒绝失败查询的部分候选、损坏扫描结果，保留不收敛错误；15 项通过。另用真实宿主 `readelf` 对 `/bin/true` 只读扫描通过，见 [解析探针](real-readelf.json)。递归补库及失败查询修复本轮主要是离线验证，没有重新下载厂商包或复验三发行版仓库。
- `native_io.py` 与原生 I/O 回归：父进程退出但同组后代仍持输出管道时，超时不再误报已清理；限时 TERM→KILL 后核验非僵尸成员。真实本地后代进程的三个边界通过；宿主不同 session 的进程不由这一局部补丁概括保证，客体清理由现有精确身份回调负责。

主控更新三个 opt-in 集成测试，使其按当前宿主准入选择客体，并使用每次新建的安装根，避免旧测试固定三客体与禁止同派系策略冲突。旧冻结证据未改写。M1 初次因测试临时共享目录未显式申请 SELinux 共享重标记而写入失败，保留原失败日志；只给本轮自有临时共享目录加 `shared-relabel` 后复验通过。

## 实机证据与保全

- [Debian/Arch 生命周期](live/m1-lifecycle.json)：真实创建、命令退出码、专用根隔离、共享文件宿主所有权、停止/启动、CLI 列表和精确清理通过。
- [Debian 资源读取](live/m4-resources.json)：停止、运行和再停止状态，以及短序列 cgroup/PSS 读取通过；不是性能比较或长期负载结果。
- [真实包文件列表](native-file-lists.json)、[清单与 CID 拒绝](real-package-identity.json) 均使用独立测试根；新容器已按本轮身份移除。
- [全仓检查日志](final-check.log)：语法、旧名转发、五个 CLI 入口及 633 项离线测试通过；5 项条件跳过包含 opt-in 容器/GPU测试，不能计作通过。本轮分别执行的实机回执见上文。
- [保全审计](preservation-audit.json)：原有 9 个容器的完整 CID、Name、Image、Mounts、State 一致，仍全部停止，无新增探针容器遗留。没有向日常 FreeCAD/腾讯会议注入故障。
- 修前源码快照：`/home/omg/文档/多兼容linux机/.lab/agents/continued-repair-20261101T154240+0800/before-source.tar.gz`，SHA-256 `29c42b313b85fb08236a71d8d653f03cb7fd2d7bf0deec9195df6348a68de04b`。本地模型接手前另有 `.lab/agents/continued-repair-20261101T154240+0800/before-local-model.tar.gz`；本轮源文件差异见 [changes.patch](changes.patch)。
- 没有暂存、提交、推送或改写历史归档。Git index 字节因读取时的 stat cache 刷新而变化，不宣称字节一致；本地任务一结束后与收尾的 stage/mode/blob/path 序列哈希一致。

下一阶段仍需真实新增仓库依赖链、完整 GUI 保存重开、媒体/账号、客体原生 Agent 桥及长压专项。当前测试成功不替代这些能力验收。
