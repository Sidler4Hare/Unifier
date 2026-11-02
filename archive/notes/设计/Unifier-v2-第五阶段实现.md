# Unifier v2 E：受限项目会话与可撤销桌面集成实现说明

日期：2026-10-25，Asia/Shanghai。分支：`codex/unifier-v2-control`。
状态：**本机受限真实验收通过：359 项回归（355 通过、4 条件跳过），三发行版项目会话、真实 PTY/信号/共享租约、原生 GIO 与撤销窗口及 C/D 组合回归均通过。以 evidence/unifier-v2-stage-e-2026-10-25/report.md 和 acceptance-result.json 为准。**

D 已验收并冻结：`file_operations.py`、`lease.py`、`registry.py` 及 D 证据未改动。

## 1. 公共入口

项目会话：

```bash
python3 tools/ml.py project run PROJECT -- ARGV...
python3 tools/ml.py project run PROJECT --cwd /abs/project/dir --env LANG -- ARGV...
python3 tools/ml.py project shell PROJECT
python3 tools/ml.py project shell PROJECT --tty

python3 tools/ml.py project session list
python3 tools/ml.py project session show SESSION_UUID
python3 tools/ml.py project session cancel SESSION_UUID --signal TERM
python3 tools/ml.py project session recover [SESSION_UUID]
python3 tools/ml.py project session retire SESSION_UUID
```

桌面集成：

```bash
python3 tools/ml.py project export PROJECT [--session SESSION_UUID] [--app APP_ID] \
  [--name NAME] [--icon ICON] [--wm-class CLASS] [--desktop-id ID] \
  [--mime-type TYPE --extension EXT] [--menu-only] [--terminal]
python3 tools/ml.py project revoke INTEGRATION_UUID
python3 tools/ml.py project integration show INTEGRATION_UUID
python3 tools/ml.py project integration list
python3 tools/ml.py project integration recover
```

桌面 `Exec` 生成的内部启动参数（不是用户需要手写的日常入口）：

```text
app open APP --project PROJECT_UUID --integration INTEGRATION_UUID \
  --app-instance APP_INSTANCE_UUID --execution-fingerprint SHA256 -- FILE
project shell PROJECT_UUID --integration INTEGRATION_UUID
```

Python 入口：

```python
ProjectSessions(root, service)
  .prepare(project_identifier, *, cwd=None, argv=None, env_keys=None,
           mode="run", integration_uuid=None)
  .create_session(project_identifier, *, argv, cwd=None, env_keys=None,
                  mode="run", tty=False, timestamp=None, integration_uuid=None)
  .run(project_identifier, argv, *, cwd=None, env_keys=None, tty=False,
       integration_uuid=None)
  .shell(project_identifier, *, cwd=None, env_keys=None, tty=None,
         integration_uuid=None)
  .execute(session_uuid)
  .show(session_uuid) / .list() / .recover([session_uuid])
  .cancel(session_uuid, signum) / .retire(session_uuid)

DesktopIntegrationManager(root, service, host_desktop=None)
  .install(project_identifier, *, session_uuid=None, app_id=None, name=None,
           icon=None, wm_class=None, mime_type=None, extension=None,
           desktop_alias=None, menu_only=False, terminal=False)
  .validate_launcher(integration_uuid, *, project_uuid=None, app_id=None,
                     app_instance_uuid=None, execution_fingerprint=None)
  .show(identifier) / .list() / .revoke(identifier) / .recover()
```

`host_desktop` 可以是注入的 `HostDesktop`；测试只使用临时 XDG 目录，不写真实
桌面条目。

## 2. 项目、作用域与文档身份

每个会话在创建和每次执行前都从项目 UUID 重新读取项目、数据根、环境记录与
环境绑定，而不是信任别名或复制记录。`prepare()` 只读：不启动容器、不建 bridge、
不 exec。会话记录固定 project/root/environment UUID、generation、container ID、
根与 cwd 的 dev/ino、argv、env key allowlist、scope 哈希与文档身份。

- cwd 必须是已登记项目根内、未被其他已登记数据根遮蔽的真实目录；根和 cwd
  的路径、dev/ino、符号链接替换都会在启动前和 exec 前再次核对。
- scope 以实际使用的同路径/显式挂载、只读选项和 same-host-object 计算 SHA-256；
  计划后挂载或只读策略改变会拒绝会话。
- 环境变量只允许显式 `--env KEY` 读取启动时的宿主值；拒绝 `PATH`、loader、
  `BASH_ENV`、`PYTHONPATH` 等，不继承宿主环境、不做命令回退。
- 文档身份 `session_user` 是持久化的数字 `uid:gid`：
  - `keep-id`：`service.uid:service.gid`（真实 fixture 为 `1000:1000`）；
  - `rootless-default`：只使用已证明的容器 root 映射 `0:0`；
  - 其他/缺失 namespace：fail closed，拒绝启动，不做 chown 补偿。
- 同一个 `session_user` 被传给 `podman exec --user` 的启动、guest probe、
  guest signal 和 guest cleanup；exec 前再次比较持久化身份，任一变化都拒绝。

## 3. Guest 执行身份与控制协议

执行仍是字面 argv 数组，不经过 `sh -c` 拼接；stdin 是管道，stderr 不混入
stdout，TTY 只在真实终端上显式申请。宿主 `podman exec` 只是代理，不能作为
执行身份或停止证明。

- 每次执行生成随机 token，并通过 `--preserve-fd` 保留独立控制 fd；
- 固定 guest shell wrapper 在 `/proc/$$/stat` 读取真实 pid、kernel start、
  pgrp、sid，先写 `READY` 再关闭控制 fd并 `exec "$@"`；
- 非 TTY 使用容器内 `setsid --wait`，让整棵命令树拥有自己的 session；
  wrapper 不依赖宿主二进制、Python 或宿主 ABI；
- 宿主只接收完整 `READY pid=... start=... pgrp=... sid=... token=...` 后才返回
  可监督执行；缺失、截断、token 不符、guest 提前退出都 fail closed；
- probe 用 guest `/proc/<pid>/stat` 的 pid+start 识别 `alive/gone/replaced`；
- signal 先复核 pid+start，只对本次执行的 session（或自身 process group）发信号，
  从不杀宿主代理来冒充 guest 取消，也不 stop 整个共享容器；
- cleanup 只处理本次执行自己的 session/process group，TERM 后复核，必要时 KILL，
  再要求“无存活成员 + leader identity 已消失”；background shell 子进程不会在
  释放 lease 后继续无跟踪运行；
- 其他 app/session 位于同一 CID 时使用同一个 per-environment `LaunchGroup`；
  手动预启动的容器不会被会话结束误停，最后一个受管 lease 仍按既有 auto-stop
  规则处理。

## 4. 取消、恢复与退出码

worker 在主线程接收 INT/TERM/HUP，只记录请求；supervisor 对真实 guest 身份
执行定向信号并按 grace 升级。宿主代理退出不是 guest 结束证明。

- 只有 guest 确实收到取消信号且 cleanup 证明该执行已结束时，退出码才规范化为
  `128 + signum`；因此真实 SIGTERM 报告 `143`，SIGKILL 报告 `137`。
- 没有取消请求的自愿 `exit 15` 保持 `15`；无法证明 guest 收到信号时也不把
  代理的 15 改成 143。
- launcher 被 SIGKILL 时，detached worker 继续持有 lease、等待 guest 结束并在
  之后释放；恢复逻辑不复用宿主 pid/stamp 作为 guest 证据。
- 恢复只在 guest pid/start 经自己的 `/proc` 身份确认 `gone` 后释放对应 token；
  guest `alive`、probe `unknown`、容器已停导致无法证明、或仍有 live worker/proxy
  时，lease 保持 unresolved，绝不把“宿主代理已死”当作 guest 已死。
- 项目会话 lease 在 group 读取时不会因宿主 worker 失效被自动丢弃；必须由
  会话恢复/取消以 guest 证明结算。其他 app 的既有 stale lease 行为不变。
- 计划但从未启动、且没有 lease/guest 的会话在 recovery 中明确失败，不伪造
  已执行状态。

## 5. 桌面集成 journal 与启动钉住

`DesktopIntegrationManager.install()` 在任何文件副作用前先写 journal，记录每个
文件的角色、绝对路径、before/after base64 与 SHA-256；状态包括
`planned/installing/installed/revoking/revoked/conflict`。

- 项目入口钉住 integration UUID 与 project UUID。
- 应用入口额外钉住 `app_instance_uuid` 和完整 execution fingerprint（id、argv、
  env、user、workdir、mounts、environment binding 等）。
- 启动时 CLI 先校验 journal 为 `installed`、项目/环境绑定仍一致、桌面文件字节
  仍等于 journal 的 after 哈希；app 入口再校验 app instance 与 fingerprint。
- detached on-demand worker 的命令行携带同一组钉子，`managed_open()` 在拿 lease
  与启动环境前再次校验；`exec_app()` 在最终 exec 边界再校验。
- journal 中记录的 desktop path 是权威位置，因此用注入 `HostDesktop` 安装的条目
  也能被另一个默认 `HostDesktop` 的进程验证；不要求在默认 XDG applications 下。
- app 被删除后同名重建、app_instance_uuid 改变时，旧 `.desktop` 的旧钉子会被
  拒绝；integration revoke 后任何旧缓存 launcher 也被拒绝。
- revoke 先做逐文件 preflight：只有当前字节仍等于 journal 的 after 才恢复 before；
  外部编辑/删除是 conflict，不覆盖、不删除外部字节。恢复 mimeapps 时写回完整
  before 字节，包含原有空行、注释与是否带尾随换行。
- revoke 在文件副作用前持久化 `revoking`；中途失败写 conflict，mixed 状态由
  `recover()` 如实报告，不伪装成干净撤销。持久的 `revoking` 意图在恢复时只会
  继续完成撤销（恢复已知 before、删除我们自己的 after），绝不会倒回 installed；
  已知部分撤销也可继续完成。冲突 journal 是粘性的，不会因后来文件字节碰巧
  匹配 after 而重新获得授权。
- MIME 关联仍只把 `%f` 作为独立的文件参数，应用参数与文件参数角色分离；
  GIO/GLib 的 Chinese/space/quote/semicolon/percent 文件名由既有 Exec quoting 保留。

## 6. 测试

新增/完善：

- `tests/test_project_sessions.py`：30 项。覆盖计划零副作用、project/root/cwd/scope
  绑定、env allowlist、keep-id 文档身份与未证明 namespace 拒绝、执行 argv/streams/
  user、自愿 exit15 与真实取消 143 的区分、user pin 变更拒绝、共享 LaunchGroup、
  live/unknown guest 保留 lease、confirmed gone 释放、planned 失败、直接 cancel
  的用户与 guest 身份、CLI 双横线解析；另有 4 项宿主本地 `setsid`/`/proc` 真实
  shell helper 协议测试（wrapper READY、probe/signal、session/group cleanup）。
- `tests/test_desktop_integrations.py`：19 项。覆盖 journal before/after、精确字节
  恢复、外部编辑 conflict、混合/失败 revoke、durable revoke 不被恢复成 installed、
  foreign desktop 拒绝、integration UUID + app instance + fingerprint 钉子、注入
  HostDesktop 的 journal 权威性、同名重建 app 的旧 launcher 拒绝、revoke 后旧缓存
  拒绝、CLI 与 detached worker 的启动前复验、`%f` 文件角色与特殊字符 Exec quoting。
- primary 注入的 `tests/test_desktop_recovery_authority.py`：3 项，验证 durable
  revoke/partial revoke/旧 installed 字节都不能在 recovery 中重新授权。

执行结果（私有日志）：

- 上述两个 E 测试文件：**49 项全部通过**；
- 加入 primary 的 recovery-authority 3 项后 targeted：**52 项全部通过**；
- `python3 -m unittest discover -s tests -v`：**358 项，OK（4 项条件跳过）**。

日志：

- `.lab/agents/unifier-v2-stage-e-20261025/unittest-e-targeted-fix.log`
- `.lab/agents/unifier-v2-stage-e-20261025/unittest-full-fix.log`

## 7. 前置条件与明确限制

前置条件：

- 当前 Linux 宿主上的 rootless Podman + crun，容器由本项目的环境记录拥有；
- 项目根是已登记的本地普通目录，cwd 在根内；
- 共享目录使用同一绝对路径（same-path share）；
- guest 至少有 `/bin/sh`、`/proc`、可用的 `setsid --wait`（TTY 路径不强制 setsid）；
- 真实镜像/容器、PTY 和 GIO 验收由主控执行，本轮未运行或修改 primary 的容器、
  workspace、fixture、snapshot 与探针。

限制：

- 仅支持受限的已登记项目会话，不做全局宿主命令拦截、发行版自动重试或任意
  后台 detach；
- 不跨宿主、不做压力/跨机器验证；
- guest 进程只在自身 session/process group 可证明时清理；无法证明时会话保留
  unresolved 而不是猜测停止；
- 不声称宿主机与 guest 的 namespace 映射是特权安全边界，也不做 chown/ACL 修复；
- 桌面集成只管理本工具 journal 的条目；外部已有条目、外部修改、冲突状态不被
  覆盖；
- 不修改宿主默认应用、宿主策略、软件包或日常 FreeCAD 数据。

## 8. 真实验收状态

**REAL ACCEPTANCE PENDING PRIMARY**：本地单元与宿主本地 shell helper 协议测试
通过，但不能替代 primary 的真实 Debian/Fedora/Arch 项目会话、共享 CID 取消、
PTY Ctrl-C/termios、真实 GLib 默认变化、GIO 文件名与旧 `.desktop` 缓存拒绝 gate。
primary 的 attempt3 六项 session case 已全部通过，但其 `source_unchanged` 因运行
期间源码变化失败，且当前源码在本轮又已变化；attempt3 desktop 停在 GIO launch
marker。两者都不得直接当作最终 accepted，仍需在本次源码上重跑。

## 最终主控复核

最后补齐了已确认 gone/无租约的 interrupted 退役终态，并以不变最终源码重跑 359 项回归和真实组合 gate。日常 FreeCAD 更新后实际菜单启动两次，6 个测试容器及 3 卷已清理，原有 27 容器与主状态/旧证据保全。实现阶段的本地测试计数不代替该最终结果；原生会话矩阵本轮均为 keep-id。
