# 三宿主首轮测试记录

测试时间：2026-10-30，Asia/Shanghai。结果为**部分通过，尚未整体验收**。本轮已结束，未安排后台循环或自动续测。

服务器通过 `agent@192.168.0.11` 登录；虚拟机复用服务器现有配置中的 `dev` 账户，Arch 地址 `192.168.122.50`，Debian 地址 `192.168.122.117`。没有新增登录密钥或修改账户权限。

## 同一份测试源码

来自当前工作树，包含未暂存及未跟踪的产品模块，不使用 10 月 28 日旧包或远端未同步分支。202 个源文件逐项校验后才执行。源码包 SHA-256：

`8b16f27702ad918dee9719ebb60784b3ebafd76413bc52eb0b44fd8dfe23fb20`

源码、文件哈希和 Git 状态分别在 `unifier-current.tar.gz`、`source-manifest.json`、`source-git-status.txt`。本轮未修改产品源码、暂存内容和旧证据。

## 已完成的真实宿主范围

| 宿主 | Python / 内核 | 已测客体 | 基础生命周期 | 离线回归原始汇总 |
|---|---|---|---|---|
| Ubuntu 实体机 | 3.12.3 / 6.8.0-147-generic | debian13 | 通过 | Ran 537 tests in 28.826s；FAILED (failures=9, errors=12, skipped=6) |
| Arch 虚拟机 | 3.14.8 / 7.2.11-arch1-1 | debian13, fedora44, arch | 通过 | Ran 550 tests in 23.461s；FAILED (failures=1, skipped=4) |
| Debian 虚拟机 | 3.13.5 / 6.12.113+deb13-cloud-amd64 | debian13 | 通过 | Ran 537 tests in 17.641s；FAILED (failures=2, skipped=7) |

生命周期逐项验证创建、真实客体身份、共享宿主内核、命令退出码、原生包查询、停止和同 CID 重开，附加未知宿主拒绝为模拟策略输入。总共只覆盖 9 个宿主/客体组合中的 5 个；Ubuntu 与 Debian 宿主上的 Fedora/Arch 客体均为 NOT_RUN。

五个组合的真实 CLI 初始化、启动、宿主写入后客体读取、客体写入后宿主读取、退出码 17 传递及运行状态 `/etc` 列举通过。停止状态的只读 FUSE `/`、`/usr` 列举通过，`/etc` 列举均失败。细项见各机 `cli-*/summary.json`，基础容器回执见 `summary.json` 的 `headless_receipt`。

本机 Fedora 上同一传输快照的离线对照为 550 项、4 跳过、无失败；不能替代上述真实宿主结果。不同宿主的构建工具/图形依赖和条件用例不同，测试总数不能直接相减得出通过数，子测试失败也不等于独立根因数。

## 已定位的问题

1. **Ubuntu 受管移动的 Python 兼容问题。** Python 3.12 的 `fcntl` 没有 `F_SETOWN_EX`；`lease.py` 直接访问它，源清理路径抛出 `AttributeError`。Ubuntu 9 个失败、12 个错误中包含该问题导致的子测试结果，原始日志完整保留。
2. **停止状态的 `/etc` 浏览失败。** `/etc/mtab` 指向 `/proc/mounts`，投影拒绝受限 `proc` 的错误未在目录项检查中妥善处理，导致整目录列举退出。五个实测组合均复现；`*-view-diagnostic.stdout` 保留底层异常。
3. **文档中的 doctor 直接入口失效。** `python3 tools/lab.py doctor` 因 `tools/unifier.py` 遮蔽同名包而导入失败。改用 `PYTHONPATH=src python3 -m tools.lab doctor` 可诊断。补齐依赖后三宿主的模块入口检查均通过，原入口仍记失败。
4. **文件变化通知测试依赖时间戳分辨率。** 快速等长改写并还原 mtime 时，Ubuntu 47/50、Debian 48/50 次的 size/mtime/ctime 签名未变化；Arch 为 0/50。写前等待 25ms 后三机均为 0/50。协议夹具已复现通知预期不成立；真实挂载缓存影响尚未专项验证，不能只给测试加等待后宣布产品通过。
5. **Arch 虚拟 GPU 能力未通过。** DMA-BUF 夹具返回 `unsupported / gbm_alloc`，虚拟渲染节点不能完成所需线性缓冲区分配。资源已释放；这不代表 V100 烤机或 GPU 透传验收结果。

初版驱动尝试在 headless 环境 `app add`，产品按桌面前置条件明确拒绝；依赖该注册的 `run` 步骤未执行有效的启动验证。该轮 `live-*` 原始失败保留，但不列为新的应用启动缺陷。后续 `cli-*` 改用正确的 `env run`，通过了真实文件互写及退出码验证；桌面 `run` 工作流仍为 NOT_RUN。

## 环境变化与收尾

- Ubuntu 由用户管理员安装 Podman/Skopeo/用户映射和网络依赖；Debian 虚拟机只新增 Skopeo 一包，没有升级或移除其他包。相关输出在安装日志及各机最终 inventory。
- Ubuntu 首次下载一套 digest 锁定的 Debian 基础镜像（锁文件记载压缩层约 29.8 MB）；虚拟机测试复用既有缓存。缓存留作后续测试，不做全局 prune。
- 全部本轮临时容器已经按自身 registry 身份正常停止并移除。本轮 FUSE 挂载残留为 0；原有 Ubuntu/Arch/Debian 容器数量分别仍为 0/1/1，CID、状态与启停时间对照均通过。两个虚拟机仍运行。
- GPU 烤机在处理请求前已自行结束，`run.log` 末尾为 `FINISHED`，两个 GPU 任务返回 0。本轮没有找到需要终止的烤机进程。最终两张 V100 利用率均为 0%，没有 CUDA 计算进程，温度为 34°C、30°C；原日志保留在服务器 `~/gpu-burnin`。这里只确认烤机已停，不作硬件质量结论。

三机的远端工作区保留在各自 HOME 的 `server-validation-20261030T204043+0800`，本地产物为此目录。产品源码修复、GUI 应用安装/保存/重开、媒体、性能及完整应用/Agent 扩大测试尚未执行或验收。

所有原始回执、驱动、失败日志和源码包均在此交付目录；`ARTIFACT-SHA256SUMS` 用于核验交付文件。
