# 本机扩展测试两处故障修复交接（2026-10-31）

## 范围与状态

本轮只修两处产品缺陷，未创建、启动或删除任何产品容器，未访问三台服务器，未改日常根 `~/.local/share/unifier`、`~/.local/share/multilinux/apps/{freecad,wemeet}`、日常两个容器或历史证据。没有执行 `git add`/`commit`/`push`/`reset`/`clean`，暂存区未动。

实机复验由主控在代码审查后派发。本轮没有打开真实 FUSE 视图，也没有运行新的 Debian/Arch 客体；结论以离线单元和 `tools/check_repo.py` 为准。

- 新增证据/日志目录：`artifacts/extension-local-20261031T111237+0800/`
- 主控提供的原生创建证据：`native-podman-mount-options.json`
- 主控提供的生命周期证据：`native-podman-mount-lifecycle.json`
- 主控首轮 canary 失败原件：`canary-attempt1/debian13-stopped-ls-1.stderr`、`canary-attempt1/steps.json`

## 故障 1：停止态 `ls` 报 `frozen share options changed`

### 根因

不是 `shared_mounts.options='z'` 记录错误，也不是要取消冻结挂载校验。真实 Podman 生命周期是：

1. `create` 后 `Mounts[].Mode='z'`、`Mounts[].Options=['rbind']`、`HostConfig.Binds` 仍有 `z`。
2. 第一次 `start` 会消费 `z`：`Mode` 变为空，`HostConfig.Binds` 也丢 `z`；`stop` 后仍为空。
3. 同一 CID 的 `Config.CreateCommand` 从 create 到 running 到 stopped 始终保留原始 `-v HOST:DEST:z`。

因此只读 `Mounts.Options` 会永久失败；只读 `Mounts.Mode` 只能覆盖未启动阶段。最终实现按“创建期选项”和“运行时属性”分层：

- `Mounts.Options` + `Mounts.Mode` + `RW` 作为运行时事实。
- `z`/`Z` 不是运行时挂载 flag。`Mode` 仍存在时直接与冻结记录精确比较。
- `Mode` 已被消费时，只对同一 `record["runtime"]["container_id"]` 查询 `Config.CreateCommand`，证明原始 `z/Z` 请求。
- 不执行 `CreateCommand`，不按冒号盲拆路径；只解析 `--volume`、`--volume=...`、`-v`、`-v=...`、`-vVALUE`，并要求卷参数与冻结记录的真实 host/destination 精确匹配。
- 创建证据缺失、不可读、无匹配、重复/歧义、`z` 与 `Z` 不符、创建选项集合与冻结记录不一致，全部 fail-closed。
- 运行时 `Source`、`Destination`、`RW` 继续严格核对；`RW=false` 是只读权威。记录期望 rw 而实际 ro、或相反，均拒绝。未知的期望 flag 仍必须出现，不被过滤。
- 返回给视图的 `options` 仍是真实运行时 flags（例如 `rbind`），不会把已消费的 `z` 伪造回运行时属性。

### 代码

`src/unifier/views.py`

- `_mount_options()`：完整读入 `Mounts[].Mode` 并保留 `Options`、`RW`，不再丢创建期 `z/Z`。
- 新增 `_option_tokens()`。
- 新增 `_creation_volume_options()`：按同一 CID 读取并解析 `Config.CreateCommand`，只做精确 host/destination 匹配。
- 新增 `_verify_mount_options()`：统一 frozen share 和 persistent mount 的 `z/Z`、ro/rw、未知 flag 校验。
- `_collect_mounts()` 对 `shared_mounts` 和 `persistent_mounts` 使用同一校验，并传入同一 CID。

### 回归

新增 `tests/test_view_mount_options.py`，使用主控回执的真实形状覆盖：

- 启动前 `Mode='z'` 正向，且不额外查询 `CreateCommand`。
- 启动后 `Mode=''`、同一 CID `CreateCommand` 仍为 `z` 正向。
- `CreateCommand` 使用 `--volume` / `--volume=` 两种形式正向。
- `Mode` 与 `CreateCommand` 的 `z`/`Z` 错配拒绝。
- 已消费 `z` 但创建证据缺失、inspect 失败、源不符、目的不符、重复卷参数歧义拒绝。
- `label=disable` 不能豁免缺失的 `z` 证据。
- ro/rw 双向错配拒绝，包括已消费 `z` 时创建请求的 ro 与记录不一致。
- 真实源替换、目的不在 Podman mounts、未知期望 flag 仍拒绝。
- 未知创建 flag（如 `z,noexec` 不在冻结记录中）拒绝。

另用主控三个阶段的原始 `native-podman-mount-lifecycle.json` 做了离线回放，日志在
`artifacts/extension-local-20261031T111237+0800/logs/lifecycle-evidence-replay.log`：
created 阶段 `Mode=z` 直接通过且不查创建命令；running/stopped 阶段空 `Mode` 只在同 CID
`CreateCommand` 证明为 `z` 时通过，且运行时 options 保持 `rbind`，不伪造 `z`。

## 故障 2：`env remove` 后快捷 `list` 仍 ready/files_mounted=true

### 根因

安装选择文件 `install-state.json` 保存用户选择和最后一次结果，但环境记录与容器身份才是权威。旧 `list/status/dirs` 直接使用 `item["state"]` 和 `item["files_mounted"]`；`env remove` 只删环境记录，没有改选择文件，于是陈旧的 `ready` 和 `files_mounted=true` 被继续输出。

### 代码

`src/unifier/shortcuts.py`

- 新增 `_target_runtime_facts()`，只读派生单个 target 的事实，不创建、不启动、不收养：
  - 环境记录不存在：`state="missing"`、`container_state="missing"`、`files_mounted=false`、`failure` 明确记录，安装选择仍保留。
  - 记录存在但 CID/UUID/宿主 target 不匹配：`state="failed"`，不报告 ready，不报告挂载为真。
  - adopted target 以 `source_root` 源记录为权威；源记录被删也报告 `missing`，不会改用同名新容器。
  - 只有记录有效且运行时为 running/stopped 时才继续报告 `files_mounted=true`。
- `_list_payload()` 使用派生事实；新增 `missing` 数组；`failed` 也包含 missing（原报告中的 `failed` 为空不再出现）；`retained` 只统计真实存在且身份匹配的容器；`enabled` 保留用户选择。
- `list`/`status` 人类输出对 missing 显示“环境记录缺失”，不再显示 ready。
- `_dirs_payload()` 同样派生；missing 时 `files_mounted=false`，人类 `dirs` 也显示 missing 和未挂载。
- `cmd_path()` 在环境记录不存在时拒绝，不再仅凭陈旧 selection 输出目录路径。
- `start`/`run`/`install`/`stop` 原有 `store.exists()` 检查保留；本轮回归确认它们失败且不会创建/启动/收养同名容器。
- `src/unifier/i18n.py` 增加 `status.environment_missing` 中英文。

### 回归

`tests/test_cli_shortcuts.py` 新增 `MissingEnvironmentStatus`：

- 正常删除环境记录后：`list` 为 missing、`files_mounted=false`、container missing、`failure` 非空、`enabled` 保留用户选择、`retained=[]`、`failed=["debian13"]`、`missing=["debian13"]`。
- 该过程不重写 `install-state.json`，不产生 create/start/remove 事件。
- `dirs --json` 同样 missing/未挂载；人类 `dirs` 明确显示环境记录缺失和未挂载。
- `path`、`start`、`install`、`run`、`stop` 均失败且无 create/start/remove。
- 修改环境记录 CID 后：`list` 为 failed，不 ready，不报告挂载；`dirs` 同样；`start` 失败且不重建。

## 验证命令与结果

所有命令使用 `LC_ALL=C LANG=C`、`PYTHONDONTWRITEBYTECODE=1`，日志在工作树内：

```bash
LC_ALL=C LANG=C PYTHONDONTWRITEBYTECODE=1 \
  python3 -m unittest tests.test_view_mount_options tests.test_cli_shortcuts \
  tests.test_share_label tests.test_views -q
# Ran 97 tests ... OK
# log: artifacts/extension-local-20261031T111237+0800/logs/targeted-unittest.log

LC_ALL=C LANG=C PYTHONDONTWRITEBYTECODE=1 python3 tools/check_repo.py
# PASS Python syntax: 182 files
# PASS legacy module aliases: 42
# PASS five CLI entry points
# Ran 592 tests ... OK (skipped=5)
# PASS repository check
# log: artifacts/extension-local-20261031T111237+0800/logs/check_repo-full.log
```

`check_repo.py` 本轮通过，没有因主控或其他未提交非本任务变动失败。它检查的是工作树，不是暂存快照；未运行 `--staged`，也未改暂存区。

生命周期证据离线回放：

```bash
# log: artifacts/extension-local-20261031T111237+0800/logs/lifecycle-evidence-replay.log
# PASS created: Mode='z' RW=True runtime_options=['rbind', 'z'] create_proof_calls=0
# PASS running: Mode='' RW=True runtime_options=['rbind'] create_proof_calls=1
# PASS stopped: Mode='' RW=True runtime_options=['rbind'] create_proof_calls=1
```

`git diff --check -- src/unifier tests` 通过。

## 风险与边界

- `CreateCommand` 证明只在 `Mode` 已空且冻结记录期望 `z/Z` 时查询；启动前 `Mode` 仍是直接证据。若未来 Podman 改成连 `CreateCommand` 也消费/改写，会明确失败而不是假装通过。
- 解析只覆盖实际使用的 `--volume`/`-v` 形式；若某版本改用 `--mount`，会因缺少创建证据而失败，不会回退到忽略校验。
- 创建证据要求卷参数与精确 host/destination 匹配。路径含 `:` 时因为 Podman 卷语法无法无歧义解析，当前 fail-closed；产品自身也拒绝含 `:` 的共享路径。
- 运行时 `Options` 仍采用“期望 flag 必须出现”的子集校验，以容纳 Podman 自动加入的 `rbind`、`nosuid`、`nodev` 等；期望中的未知 flag 不会被过滤。已消费 `z` 时创建选项集合要求与冻结记录一致，额外创建 flag 会拒绝。
- `list` 的 `failed` 现在包含 missing，同时新增 `missing` 数组；调用方若只读 `failed` 会看到丢失环境，读 `missing` 可区分“丢失”与普通失败。`enabled` 保留用户选择，不代表环境可用。
- `path` 在环境记录被删后返回未配置，即使 `files/` 目录仍留在磁盘；本轮没有删除任何专用目录或数据。
- 离线测试不能替代真实 FUSE/挂载生命周期。主控实机驱动仍需验证 stop 后 `ls`、两轮保存/停机/重开，以及 remove 后 `list` missing。

## 实机复验怎么跑

主控驱动 `artifacts/extension-local-20261031T111237+0800/primary_live.py` 未改动，由主控执行。复验时建议保留以下检查点：

1. 短期 init fixture 用 `bin/unifier`，显式 `UNIFIER_HOME`，worker 根一致；确认 `shared_mounts.options='z'`、`podman inspect` 同一 CID 的 `Config.CreateCommand` 含原始 `:z`。
2. 私有 `files` 两轮普通用户保存、停机、重开；停止态 `ls` 应通过，不再出现 `frozen share options changed`。若失败，先抓 `podman inspect --format '{{json .Mounts}}' <CID>` 和同 CID 的 `Config.CreateCommand`，不要改冻结记录或测试期望。
3. 走正常 `stop` 和 `env remove` 后，快捷 `list`/`status` 必须显示 missing、`files_mounted=false`、容器 missing；`start`/`run`/`install` 必须失败且无 create/start。
4. 清理只用精确 CID 匹配后 remove；不 prune，不强停未知租约，不碰日常两个容器和 `~/.local/share/unifier`。
