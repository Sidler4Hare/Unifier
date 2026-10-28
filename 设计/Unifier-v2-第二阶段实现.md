# Unifier v2 第二阶段 B：项目、处理器与受限文件路由

日期：2026-10-24，Asia/Shanghai。分支：`codex/unifier-v2-control`。状态：**B 首版受限闭环已接入并完成本机 Arch FreeCAD 真实 GUI 验收；三发行版原生复验通过。**

本阶段把已登记的宿主项目、FileRef、环境应用接到一个用户级控制门面。日常调用形如：

```bash
python3 tools/unifier.py file plan --path /host/project/part.FCStd --intent edit
python3 tools/unifier.py file open --path /host/project/part.FCStd --intent edit
```

`plan` 是可审查快照；`open` 不读取用户提交的旧 plan JSON，而是从路径和 intent 重新解析项目、处理器、应用实例、环境代际、挂载映射与文件观测。

## 1. 显式 Project 登记

`project add` 才产生执行授权，不从复制来的项目配置自动继承。记录至少包含：

- `project_uuid`、可选 `alias`、`root_uuid`；
- `default_save_root`：项目根内的绝对目录；
- `access_policy`：`read-only`/`read-write`；
- `environment_binding` 与 `toolchain`：固定 `environment_id`、`environment_uuid`、`generation`、`container_id` 和目标；
- `intent_handlers`：`edit`/`view` 到稳定 handler UUID 的映射。

一个数据根最多登记一个项目，避免路径路由歧义；删除 project 只删除登记，不删除根、应用、环境或数据。

```bash
python3 tools/unifier.py project add \
  --root ROOT_UUID --environment arch --alias demo \
  --default-save-root /host/project --policy read-write \
  --handler edit=HANDLER_UUID
python3 tools/unifier.py project list
python3 tools/unifier.py project show demo
python3 tools/unifier.py project remove demo
```

## 2. 显式 Handler 登记

首版 provider 只有 `environment-app`。handler 至少包含：

- `handler_uuid`、可选 `alias`、`intent`（`edit`/`view`）、`provider`；
- `app_id` 与 `app_instance_uuid`：**新 `app add` 独立生成的实例 UUID**，同名 app 删除重建后不同；
- `app_binding`：环境 UUID/代际/容器实例；
- `execution_fingerprint`：对 launch 的 executable、argv、env、user、workdir、on_demand、mounts、package_origin、app instance 等做 SHA-256；
- `file_argument`：首版 `append`（应用 argv 后追加文件绝对路径）或完整 `argv` 模板，模板中必须恰好有一个独立的 `{file}` 元素；
- `save_capability`：`explicit-path`、`dialog-adapter` 或 `unknown`。

旧 launch 没有 `app_instance_uuid` 时仍可 `app show/open` 和原有桌面路径使用，但不能登记新 handler；必须显式 remove/add app。没有执行配置指纹或实例的运行记录不能静默绑定。删除 handler 只删除登记，不删除 app/环境/数据。

```bash
python3 tools/unifier.py handler add freecad \
  --intent edit --save-capability explicit-path --alias freecad-edit \
  --argv-template /usr/bin/FreeCAD --argv-template '{file}'
python3 tools/unifier.py handler list
python3 tools/unifier.py handler show freecad-edit
python3 tools/unifier.py handler remove freecad-edit
```

`{file}` 是字面 argv 元素，不拼入 shell/命令字符串；文件名中的空格、中文、引号和分号仍只占一个参数。模板第一个元素必须等于应用 executable，不能借模板改变 executable。

## 3. 路由与计划

`file plan/open` 使用 A 的锚定 FD FileRef 与最长嵌套根规则，然后：

1. 按 FileRef 的 `root_uuid` 找唯一 project；无 project 或无对应 intent handler 直接失败；
2. 校验 handler 的稳定 UUID、app instance、执行配置指纹、app 环境绑定和 project 工具链绑定；
3. 校验环境记录/实际容器实例、root 原 inode、默认保存目录；
4. 按运行时 **container path** 的挂载覆盖优先级，分别解析 project root、文件路径和 cwd；最具体挂载若把同一 container path 指到不同 host 对象，立即拒绝，不用父挂载 fallback；
5. `edit` 要求 project 与 root 都是 `read-write`，且实际使用的挂载中没有 `ro`；
6. readonly policy 需要实际使用的所有挂载都是 OS 层只读，否则 `view` 也拒绝，避免把策略标签冒充隔离；
7. 文件参数使用规范宿主绝对路径；当前环境必须把该宿主目录以相同绝对路径挂载，否则拒绝。

计划明确输出：provider、project/root/handler 的 UUID、`environment_uuid`/`generation`/`container_id`、`app_id`/`app_instance_uuid`、`observed_version`、`argv`、`cwd`、`intent`、`save_capability`，以及真实的 `access_scope`。`access_scope` 列出所有静态共享挂载、root/file/cwd 的 container→host 映射、最具体选中挂载和 `policy_enforcement: routing-only`。

同一环境的所有应用共享全部静态挂载；broker 的一次路由检查或一个 FileRef 不是单文件、单应用隔离。若环境还挂载了项目之外的目录，计划会列出实际范围。

`default_save_root` 是项目上下文的一部分。计划同时输出 `save_as_default: unknown` 和 `default_save_enforced: false`：只设置 cwd 不能证明应用的新建 Save As 默认目录已经生效；只有监督者的真实 QAction/QFileDialog 才能验收。

## 4. 执行与生命周期

- 非 on-demand app：`EnvironmentService.exec_app()` 使用 argv 数组、显式 workdir 和固定 app instance/执行指纹；在 start 生命周期锁内和 exec 边界重新核对。
- on-demand app：`file open` 主入口只做只读预检，然后启动 detached `file _worker`。worker 从 path/intent 重新解析全部绑定，持 `LaunchGroup` 租约并执行；不会读取旧 plan JSON。`MULTILINUX_FILE_WORKER` 防止递归监督。
- 启动器被 SIGKILL 后 worker 继续持有租约；应用退出后由 worker 释放并停止它启动的环境。该路径沿用已有 supervisor 生命周期，不再用前台 `managed_exec` 替代。
- 不启动每个进程的新发行版，不拼 shell，不自动安装包或切换环境；没有宿主解释器、MIME 或其他发行版 fallback。

## 5. 评审反例修复

- 同名同配置 app 重建：新增独立 `app_instance_uuid`；固定 clock 下仍不同。handler 固定实例，旧 handler 拒绝。
- detached worker：on-demand `file open` 只做预检后分派 worker；worker 重新解析并持租约；退出码/stdin/stdout/stderr 继承。
- 检查/执行间隙：把 app instance 与执行指纹传入 `start`/`exec_app`/`managed_exec`，在生命周期锁和 exec 边界复核，不靠重复 `_resolve` 宣称安全。
- readonly + rw mount：readonly policy 的 view 也要求所有实际使用挂载为 `ro`，否则拒绝。
- 默认保存目录：按真实路径和全部已登记根校验；进入嵌套根或含符号链接的默认目录拒绝。
- 挂载遮蔽：按 container path 最长前缀解析 root/file/cwd；不同 host 映射或同级歧义拒绝，子 ro 挂载参与 edit 拒绝。
- 模板：`{file}` 必须是独立 argv 元素；直接 API 错类型、重复 UUID/alias 均报 `MultilinuxError`，注册表不随机取第一项。
- 生命周期等待后竞态：`start()` 在 `_wait_while_stopping` 返回后重新读取环境记录和应用记录，并在任何 `runtime.start`/capture/`store.save` 前复核 app instance、执行指纹和绑定；保存时合并最新记录，不用等待前旧 record 覆盖新状态。等待期间切换 app.env 或环境代际/实例会在启动前拒绝。

## 6. 测试与边界

最终回归 **236 项：232 通过、4 项按条件跳过**；命令与输出见 `evidence/unifier-v2-stage-b-2026-10-24/report.md` 与 `finalize-regression.txt`。新增路由回归使用临时目录和 `FakeRuntime`。监督者已完成 `live-freecad/` 的真实 Wayland QAction/QFileDialog、原路径保存/新建/停机重开、launcher SIGKILL 后回收和原生 pacman 卸载后数据保留；38 项证据检查通过，详见本轮报告。

仍未实现：C 的停止环境 FUSE 投影；D 的受管移动、源清理和断电恢复；E 的通用无前缀工具链与桌面入口撤销；F 的跨宿主/压力矩阵。本次 Save As 默认位置只覆盖先打开宿主原件再新建的实际 FreeCAD 流程，通用计划仍为 unknown；卸载后已验证数据保留与移除 app 登记后路由拒绝，未承诺任意包变更后的通用 handler 自动修复。
