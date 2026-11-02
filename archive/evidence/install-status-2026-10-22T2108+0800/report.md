# 安装状态准确性：维护钩子与系统集成分层状态

2026-10-22，Fedora 44 KDE 宿主，内核 `7.2.11-200.fc44.x86_64`，rootless Podman 5.8.9。
证据目录：`evidence/install-status-2026-10-22T2108+0800/`。产品代码快照的 git 状态见 `manifest.json`；
本轮所有事务都由快照内的 `project/tools/ml.py` 子进程执行，不是进程内模拟。

**判定：`layered-status-verified`。** 3 个真实 Arch 事务全部按分层语义记录：包已入库、命令可运行、
维护钩子失败和系统集成未验证分别在 `maintenance` 与 `verdict` 中可见，不再把“pacman 退出 0”
呈现为完整成功；宿主包清单和桌面入口前后不变，实验环境已停止。这不是 Sunshine 完整兼容验收。

## 1. 修复前的现场问题

`evidence/arch-complex-2026-10-22T202359+0800/` 的 Sunshine 本地包事务 `txn7bf199dc48dd`：

- `pacman -U` 退出码 0，`sunshine` 已入库，`sunshine --version` 退出码 0；
- stderr 有 2 条 `error: command failed to execute correctly`；
- stdout 有 udev 写 `uevent` 权限拒绝、`modprobe: FATAL`、systemd 未作为 PID 1 等输出；
- 事务仍然只有 `phase=completed`，没有维护钩子或系统集成字段，看起来像完整成功。

## 2. 新的事务分层

事务保留原有的原生命令执行语义：

- `phase`：`completed` / `failed`，只表示包事务本身是否完成；包已安装时不会因为维护钩子失败而降为 `failed`。
- `maintenance`：`{status: "ok"|"warnings", items: [...], system_integration: "unverified", full_init: false}`。
  每个 item 记录 `source`、`reason`、`tool`、截断后的原始行和 `count`，按 (source, tool, reason) 合并计数。
- `verdict`：`verified` / `verified-with-warnings` / `failed`。`phase=completed` 且有维护钩子失败时为
  `verified-with-warnings`；旧事务没有这些字段时，`pkg log` 按 phase 派生 verdict。

检测扫描完整 stdout/stderr（不是只扫描保存的尾部），覆盖本轮现场可见的明确失败模式：
pacman `command failed to execute correctly`、rpm/dnf scriptlet 失败、`modprobe: FATAL`、
systemd 未作为 PID 1、system scoped bus 不可用、udev `uevent` 写入失败。
普通容器条件（`Skipped: Current root is not booted.`、含 `ignoring` 的行）和普通 `error:` 不误报。

## 3. 真机方法

1. 从已停止的 `complex-arch` 证据环境按精确 ID 执行 `podman commit --pause=false`，得到本地镜像
   `localhost/multilinux/installstatus-arch:20261022210845`（digest `sha256:9733d4d0…`）。源容器未安装新包。
2. 新建独立项目快照与 `install-arch` 环境（`--network pasta`，无桌面），从该镜像创建；镜像摘要写入
   `manifest.json` 和 `project/config/images.lock.json`。
3. 构造两个本地 Arch 包：
   - `ml-benign-1.0.1-1`：无失败钩子，用于正常路径；
   - `ml-hooktest-1.0.1-1`：安装一个 `PostTransaction Exec=/bin/false` 的 alpm 钩子。真实 pacman 仍退出 0，
     stderr 写 `error: command failed to execute correctly`。
4. 每个用例都先删除并重建 `install-arch`（同版本重装不会再次执行包自带的 Install 钩子，因此不能复用脏环境），
   再用 `project/tools/ml.py pkg install --file ...` 执行事务；CLI 退出码、stderr 警告、事务 JSON、
   `pkg show`、`pkg log` 全部保留。
5. 真实 Sunshine 用例使用 arch-complex 保留的原始 `sunshine-2026.914.233613-1-x86_64.pkg.tar.zst`
   （SHA-256 `17eb8c4d…`），执行 `sunshine --version` 作为 use command。

## 4. 结果

| 用例 | 包/命令 | pacman 退出 | 事务 phase | verdict | maintenance | 失败模式 |
|---|---|---|---:|---|---|---|
| 正常路径 | ml-benign + `/bin/true` | 0 | completed | verified | ok | 无 |
| 合成失败钩子 | ml-hooktest + `/bin/true` | 0 | completed | **verified-with-warnings** | warnings | pacman-hook-failed ×1 |
| 真实 Sunshine | Sunshine + `sunshine --version` | 0 | completed | **verified-with-warnings** | warnings | udev-uevent-write-failed ×43、modprobe-fatal ×3、systemd-not-pid1 ×1、systemd-system-bus-unavailable ×1、pacman-hook-failed ×2 |

三个用例的 `verification.ok` 都为 true，包均在清单中、use command 退出 0；由此仍不能推导系统服务可用。
Sunshine 用例只证明：包已安装、命令入口可运行、维护钩子失败现在是可见的；设备、uinput/uhid、systemd 服务和串流仍未验收。

CLI 行为：

- 正常路径：退出码 0，stderr 无 warning；
- 两个 warnings 用例：退出码 0（包已安装），stderr 打印一行
  `ml: warning: ... maintenance hook failure(s) ... system integration is unverified ...`；
- `pkg log` 行新增 `verdict` 与 `maintenance`；`pkg show` 原样输出完整事务。

## 5. 证据与审计

- `manifest.json`：源容器、镜像 digest、项目快照文件 SHA-256、git 状态、包 SHA-256、环境记录；
- `cases/{benign,hook-failure,sunshine-real}/`：`transaction.json`、`cli.stdout.txt`、`cli.stderr.txt`、`pkg-log.json`、`pkg-show.json`、`result.json`；
- `summary.json`、`validation.json`：`measurement_valid=true`、`product_verdict=layered-status-verified`、
  `host_unchanged=true`、`environment_stopped=true`；
- `environment.json`、`environment-packages.txt`：实验环境记录与基线包清单；
- `SHA256SUMS`：目录内文件校验值。

宿主隔离：`HostObserver` 在全部用例前后各采一次，RPM 清单（2522 条）SHA-256 与桌面入口名单均不变。
实验环境 `install-arch` 在事务结束后处于 `stopped`，`resources.exit_code=143`（SIGTERM 正常退出），无租约文件。

## 6. 边界

- 只验证 Arch 目标；Debian/Fedora 的 rpm scriptlet 和 dpkg 文本规则只有离线测试，没有真实包事务。
- 合成包故意用失败钩子验证“退出 0 但有维护失败”的检测，不代表真实软件都应以同样的钩子失败。
- 容器没有完整 init/systemd/udev/modprobe 集成，`system_integration` 恒为 `unverified`；真实站不能据此宣称系统集成成功。
- 本轮没有重新构建 Sunshine、没有启动串流、没有修改 complex-arch 证据目录中的包内容。
- 证据使用的代码快照在第一次事务前刷新为当前冻结工作树；之后只有 `tools/probe_lifecycle.py`（生命周期验证工具，
  不参与安装事务）在别处继续修改，不影响本目录事务结果。

## 7. 复现

```bash
# 准备（提交已停止的 complex-arch 证据容器并创建新环境）
python3 tools/prepare_install_status.py --run-dir evidence/install-status-<新时间戳>
# 用当前代码刷新快照、重建干净环境并执行三个用例
python3 tools/probe_install_status.py --run-dir evidence/install-status-<新时间戳> \
  --refresh-project --fresh-environment
```

每个用例都会重新执行真实事务；本工具不提供“只重算汇总”的捷径，避免汇总与原始事务脱节。
