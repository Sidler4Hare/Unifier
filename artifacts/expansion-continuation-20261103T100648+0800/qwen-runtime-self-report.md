此文件为本地模型自述，验收以主控独立回执为准。

已完成修改与验证。以下是交接。

## 根因与修复

**根因**：`exec_argv()` 发出的是单数 `--preserve-fd <宿主FD>`（Podman 5/crun 才有）。Ubuntu 24.04 的 Podman（runc）只认 `--preserve-fds N`，直接 "unknown flag" 退出 → 控制管道无人写入 → `_read_guest_identity` 读 EOF → `guest execution exited before reporting its identity`，DNF 从未执行。

**src/unifier/runtime.py**（+63/−16）
- `exec_argv(..., preserve_fd=)` → `control_fd=`：只接受 `CONTROL_FD`（3），输出 **仅** `--preserve-fds 1`；任何其它值（4、7、2、True…）抛 `UnifierError`，不再降级。`CONTROL_FD = CONTROL_GUEST_FD` 单一来源。
- 新增 `host_exec_command(write_fd, command)`：把整条 podman exec 交给宿主 helper；`package_exec`（原生包事务/DNF）与 `start_session_exec` 都走它，`guest_session_argv(control_fd=CONTROL_FD)` 改为容器侧固定 3。
- `Popen` 仍然只 `pass_fds=(write_fd,)`；删掉 `start_session_exec` 里父进程 `os.set_inheritable(write_fd, True)`（多线程下会把该管道泄给别的线程的 subprocess；实测 pass_fds 在子进程内继承足够，且 close_fds 连"可继承"的无关 FD 也一并关掉）。
- READY/ERROR 协议、`setsid --wait`、probe/signal/cleanup/cancel、needs-recovery、`_kill_proxy_group` 全部未改；未动 `agent_bridge.py`、`native_io.py`、`project_sessions.py`。

**src/unifier/exec_control.py**（新增，133 行，只 `import os, sys`）
`<fd> -- <program>…`：`os.dup2(fd, 3, inheritable=True)` → 关闭原 FD（fd==3 时只清 CLOEXEC，不自关）→ `os.execvp`。fd 未开、FD 3 已被占用、argv 畸形一律 stderr + exit 125，绝不运行 program。已在 fork 之后（close_fds 完成）执行，宿主父进程不 dup2、无 `preexec_fn`；exec 保持同一 PID/退出码/信号。按 `sys.executable + 绝对路径` 调用，**不依赖 worker PYTHONPATH**（`python3 -m unifier.exec_control` 也实测可用）。

## 测试（tests/test_runtime_control.py，新增 835 行 / 18 测试）
用一个只认真实 `--preserve-fds 1`、审计 `/proc/self/fd` 的假 podman + 真 guest 脚本（setsid、/proc）驱动：
- **argv 形状**：只有 `("--preserve-fds","1")`，绝无 `--preserve-fd`/`=1`/`=3`；tty/user 变体同样；probe/signal/cleanup 类 exec 完全不继承；非 3 控制 fd 全部 fail closed；guest wrapper 参数为 `"3"`；helper 包装形状与文件路径可达。
- **真实 helper 子进程**：pipe FD≈64 时控制数据只经 FD 3（宿主读回 `CONTROL-PAYLOAD`，子进程 stdout=`STDOUT-OK`、stderr=`STDERR-OK` 分离，payload 不在任一流）；审计显示只有 FD 3（EXTRA 无 `pipe:[…]`、原 pipe 号已关）、额外 `inheritable` sentinel 报 `closed`；退出码 9 与自我 SIGTERM→`-15`（证明 exec 不是 fork）；坏 fd/坏 argv 不运行 program；无 PYTHONPATH 环境按路径可跑；`remap_control_fd` 守护（未开报错、FD 3 占用不遮蔽、`get_inheritable(3)` 为真、原号关闭）；调用方本身就把 pipe 放在 FD 3 时仍正常。
- **真实握手**：`start_session_exec` 得到真实 pid/start/pgrp/sid（与 `/proc/<pid>/stat` 的 start 相符），取消 → `signal_guest="signalled"` → `probe_guest="gone"`；READY/token 不出现在 stdout/stderr；包事务（DNF 路径）双流分离 + 真实身份；`reject` 路径仍 fail closed 且 proxy 已回收、控制管道无 FD 泄漏；`hang` 超时 → killpg，rc=`-SIGKILL`；运行期不改写调用方自己的 FD 3。
- **变异验证**：去掉 helper → 握手测试失败；argv 改回 `--preserve-fd 3` → 2 个测试被抓；在调用方 dup2 FD 3 → 不变量测试失败（说明断言非空洞）。

`tests/test_project_sessions.py` **无需改动**就通过（其 `guest_session_argv(control_fd=<本地fd>)` 身份断言保留），模块名即 `test_runtime_control`，无改名。

## 测试数字
- 指定命令 `env PYTHONDONTWRITEBYTECODE=1 LC_ALL=C LANG=C PYTHONPATH=src:tests python3 -m unittest test_runtime_control test_project_sessions test_package_recovery -v` → **Ran 55 tests, OK**（18 + 30 + 7，3.7 s）。
- 扩大集（再加 test_env, test_app_user, test_session_recovery_contract, test_lifecycle_contracts, test_dependencies, test_agent_bridge, test_agent_cli, test_native_io, test_startup_trace, test_quality_regressions, test_release_regressions, test_cli_guest_paths）→ **Ran 174 tests, OK**（16.9 s）。

## 待真机复核 / 注意
- 本沙箱 `podman info` 失败（`chmod /run/user/1000/libpod: read-only file system`），因此未跑真容器：请在 24.04 服务器实跑一次 `project run` 与一次 DNF 安装，确认新 argv 与 READY 握手。
- 未做 git 写操作、未访问凭据/网络；未读全仓（≤6 文件）。仓库内有并发进程在写 `tools/guarded_test.py`、`tests/test_guarded_test.py`、`src/unifier/cli.py`，均非本次改动。