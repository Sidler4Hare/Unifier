# Unifier v2 第二阶段 B 证据与验收（2026-10-24）

分支：`codex/unifier-v2-control`；基线：`9a86520`。状态：**B 首版受限文件路由已实现，离线回归通过；真实 FreeCAD QAction/QFileDialog 闭环由监督者执行。**

## 范围

本阶段连接：

```text
registered data root -> registered host project -> stable handler -> environment app
-> FileRef -> file plan/open -> existing environment/on-demand lifecycle
```

日常入口：

```bash
python3 tools/unifier.py project add --root ROOT_UUID --environment arch \
  --alias demo --default-save-root /host/project --policy read-write \
  --handler edit=HANDLER_UUID
python3 tools/unifier.py handler add freecad --intent edit \
  --save-capability explicit-path --alias freecad-edit \
  --argv-template /usr/bin/freecad --argv-template '{file}'
python3 tools/unifier.py file plan --path /host/project/part.FCStd --intent edit
python3 tools/unifier.py file open --path /host/project/part.FCStd --intent edit
```

`project add` 和 `handler add` 都是显式登记；复制项目配置不会自动取得执行权。

## 实现要点

- **Project**：`project_uuid`、root、default save root、read/write policy、环境 UUID/代际/容器实例、目标，以及 `edit`/`view` 到 handler UUID 的映射。一个 root 一个 project；删除登记不删除数据、包、容器或 handler。
- **Handler**：首版 provider 仅 `environment-app`；固定 `app_id`、独立 `app_instance_uuid`、app 环境绑定、exec 配置指纹（argv/executable/env/user/workdir/on_demand/mounts/package_origin）和 `save_capability`。同名同配置 app 重建仍得到新实例；旧 handler 拒绝。同名 handler UUID 重建不替换 project 授权。
- **File plan/open**：使用 A 的锚定 FileRef；校验 project/root/handler/app instance/环境代际/root inode/default_save_root。默认保存目录进入嵌套数据根或经符号链接解析时拒绝。
- **实际挂载映射**：按运行时 **container path** 最长前缀解析 project root、file、cwd；不同 host 对象遮蔽、同级歧义、子 ro 覆盖和缺失同路径挂载均拒绝。计划列出所有静态挂载和实际映射，声明 environment-shared 范围，不声称单文件/单应用隔离。
- **策略**：`edit` 要求 project/root read-write 且实际使用挂载非 ro；readonly policy 的 `view` 要求所有实际使用挂载 OS 只读，否则拒绝，不用 warning 代替契约。
- **执行**：argv 数组，无 shell。`{file}` 只允许一个独立 argv 元素，中文/空格/引号/分号仍是单参数。on-demand `file open` 只做只读预检，随后启动 detached `file _worker`；worker 从 path/intent 重新解析、持租约并执行，不使用旧 plan JSON，`MULTILINUX_FILE_WORKER` 防止递归监督。
- **执行边界**：app instance 与执行指纹传入 `start`/`exec_app`/`managed_exec`，在生命周期锁、等待结束和 exec 边界复核。
- **等待后竞态**：`_wait_while_stopping` 返回后重新读取 app/env 记录，再核对实例、指纹和绑定；任何 `runtime.start`/capture/save 前完成。保存使用最新 record，避免旧记录覆盖新状态。等待期间修改 `app.env` 或环境代际会在启动前被拒绝。
- **默认保存**：cwd 设为 project default，但 `save_as_default` 明确为 `unknown`、`default_save_enforced=false`；不宣称应用 Save As 默认位置已生效。
- **注册表**：projects/handlers 使用原子 JSON + flock；重复 UUID/alias 在读取/写入时拒绝；直接 API 错类型返回 `MultilinuxError`。

## 验证

最终全量回归：

```text
Ran 236 tests in 7.340s
OK (skipped=4)
```

即 232 通过、4 项既有现场集成测试按条件跳过。完整输出见 `finalize-regression.txt`；新增测试主要在 `tests/test_file_routes.py`，并保留 A 的反例回归。

新增测试覆盖：无 project/handler、readonly/rw policy、默认保存越界/嵌套根/符号链接、根替换、环境代际/实例变化、app 执行配置变化、同名同配置 app 重建、同名 handler 重建、plan 后绑定变化、等待后 app.env/环境变化、shadowing host mount、子 ro mount、中文/空格/引号/分号 argv、模板 token、重复 UUID/alias、CLI plan/open 返回码、supervisor 分派、worker 失败零副作用、删除登记/环境后数据保留、单环境复用。

## 真实 GUI 与外部验收

本调用**没有启动 GUI 或 Podman**。监督者使用已准备的 `live-freecad/` 目录执行真实 FreeCAD QAction/QFileDialog、launcher SIGKILL 后关窗回收和 native package uninstall 后数据保留；这些结果不包含在本报告的离线结论中。`live-freecad/` 现有文件未修改。

A 阶段三发行版真实验收已由监督者独立完成（`evidence/unifier-v2-stage-a-2026-10-23/live-three-distros-2026-10-24.json`、`live-postconditions-2026-10-24.json`）。监督者独立边界记录 `independent-boundaries-2026-10-24.json` 显示 7 项 routing 反例全部通过，包括 app 实例重建、readonly view、嵌套默认保存、shadowing mount、子 ro 和等待后 app 配置变化零 start/exec。

## 边界

- C：停止环境 FUSE/只读管理投影未实现。
- D：受管复制/移动、源清理、断电恢复未实现。
- E：通用无前缀工具链、桌面入口撤销未实现。
- F：压力、跨宿主验收未实现。
- 真实 FreeCAD Save As 默认目录、GUI 退出/停机、SIGKILL launcher 回收和包卸载后数据保留由监督者现场验收。
- 未提交、未推送；未改历史 evidence、冻结清单、旧容器或旧 `.lab/state`。
