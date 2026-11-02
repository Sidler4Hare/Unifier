# Unifier v2 第一阶段 A 证据与验收（2026-10-24 finalize）

日期：2026-10-24，Asia/Shanghai（初版 2026-10-23）。分支：`codex/unifier-v2-control`；基线：`9a86520`。状态：**第一阶段 A 可运行，独立复审反例已修复，普通共享目录标签政策已落地；真实 GUI/投影/复制移动等 B–F 业务未完成。**

## 快照与取代关系

- `report-2026-10-23.md`、`regression.txt`、`cli-help-2026-10-23.txt` 是 2026-10-23 的先前快照，保留不删。
- 本文件是 2026-10-24 finalize 的主报告；`finalize-regression.txt`、`finalize-cli-help.txt` 是最新代码/CLI 证据，取代先前 `regression.txt` 与 `cli-help.txt` 作为当前结果。
- 本目录中的 `live-three-distros-2026-10-24.json`、`live-postconditions-2026-10-24.json`、`independent-review-2026-10-24.json`、`mount-boundary-2026-10-24.json` 和对应 probe 脚本是监督者独立验证留下的材料，不由本次调用生成或修改；真实 Podman 状态和最终验收以其独立文件/报告为准。`independent-review-2026-10-24.json` 记录 28 项复审边界 `all_passed=true`。
- 历史 evidence、冻结清单、原实验报告和备份未修改；没有保存模型推理、凭据或现有 `.lab/state` 内容。

## 2026-10-24 复审修复

1. **身份语义**
   - 只有 schema 1 且 `environment_uuid`、`generation` 两个键**完全不存在**的旧记录可迁移。
   - 显式 `null`（一个或两个）、半身份、非法 UUID/generation、schema 2 缺身份、未来 schema 均拒绝并保持原字节。
   - 迁移前校验完整旧记录：runtime 容器实例、`writable_revision` 一致性、基础镜像 digest、包清单摘要、共享挂载、observed、创建者字段等；只有 `id`/`target` 的伪记录不能升级。
   - 迁移 runtime 枚举字段先检查字符串/安全比较，list/dict/None 返回 `MultilinuxError`；CLI 不输出 traceback。

2. **源哈希备份**
   - 备份文件为 `environments/backups/ID.pre-identity-<sha256>.json`，按本次源字节哈希命名。
   - 同一源幂等复用；删除旧环境后导入另一份同名 legacy 记录会生成第二份独立备份，两个原件均可恢复。

3. **FileRef 政策一致性**
   - 显式父根引用进入已登记子根时拒绝，要求改用子根 UUID；`--path` 仍按最长匹配选子根。旧父根引用保持历史观测。
   - 普通文件 `nlink>1` 在跨根别名核对实现前保守拒绝；`nlink=1` 不受影响。
   - 用已打开 FD 的 `/proc/self/fdinfo/<fd>` `mnt_id` 比较根、中间目录和最终对象；同设备 bind mount 也会拒绝。`mnt_id` 不可读/缺失时明确拒绝能力，不静默退回 `st_dev`。
   - 真实私有 mount namespace 探针复验：`same_device=true, refused=true`，见 `mount-boundary-probe.txt`。

4. **注册表类型安全**
   - 持久 `backend`/`management_domain`/`access_policy` 及 `add` 入参先验证字符串，再验证枚举；list/dict/None 返回 `MultilinuxError`，不改源注册表。
   - `role` 同样先验证类型/格式。

## 普通共享目录标签政策

- 新创建环境默认 `preserve-host`：普通共享目录挂载 argv 为 `host:container`，不加 `:z`/`:Z`，不改变宿主标签，不自动 `label=disable`。
- 仅显式 `--share-label shared-relabel`（或 `share_label_policy="shared-relabel"`）添加 `:z`。
- 政策持久在 `runtime.share_label_policy` 与 `shared_mounts[].options`（`""`/`"z"`）。
- `env plan --share-label ...` 将政策变化判为 `replace`，必须新建环境；现有容器/记录不原地改挂载。
- 旧记录缺该字段时按 `shared_mounts[].options` 推断：含 `z`/`Z` 为 `shared-relabel`，否则 `preserve-host`。`inspect`/`plan`/`migrate` 不写回该字段。
- desktop 仍使用既有 `selinux_label=disable` 会话机制；没有修改宿主 SELinux 或全局配置。
- enforcing 宿主上 `preserve-host` 若被拒绝，应暴露真实挂载/SELinux 错误，不 fallback 到 relabel 或自动禁用。

## 验证命令与结果

```bash
python3 -m unittest discover -s tests -v
python3 tools/unifier.py --help
python3 tools/unifier.py env create --help
python3 tools/unifier.py env plan --help
python3 tools/unifier.py root --help
python3 tools/unifier.py file resolve --help
```

最终结果：

- `Ran 203 tests ... OK (skipped=4)`，退出码 0；即 **199 通过、4 项按条件跳过**。
- 完整输出见 `finalize-regression.txt`；CLI 帮助见 `finalize-cli-help.txt`。
- 4 项跳过均为既有现场集成测试条件未满足；本次新增测试未跳过。
- 本调用没有启动真实 Podman；真实 Debian/Fedora/Arch 容器验证由监督者执行并以其独立证据为准。同目录监督者独立记录 `live-three-distros-2026-10-24.json` 显示三发行版 `passed=true`、临时容器已移除；`independent-review-2026-10-24.json` 的 28 项边界检查 `all_passed=true`。这些文件不由本调用生成或修改，最终以监督者报告为准。

新增/更新测试文件：

- `tests/test_identity_migration.py`
- `tests/test_app_binding.py`
- `tests/test_data_roots.py`
- `tests/test_file_control.py`
- `tests/test_share_label.py`

原有 `tests/test_catalog.py` 等契约测试保留并通过。

## 已知边界与风险

- **B–F 业务未完成**：真实 GUI 宿主项目闭环、停止环境只读投影/FUSE、受管复制/移动/源清理、handler 执行、daemon IPC/UI、项目会话/桌面入口撤销、同 UUID generation 切换、压力/跨宿主验收。
- 普通共享目录默认 `:z` 重标记缺口已由显式 `preserve-host`/`shared-relabel` 政策关闭；更细的 SELinux 宿主适配、enforcing 现场矩阵和策略组合仍需真实验证。
- `FileRef` 仅观察/解析；没有读取内容、写入、复制、移动、删除或执行能力。
- 根替换检测存在打开前后竞态窗口；它不是全局文件系统保证，也不限制同容器其他应用或外部写入者。
- 第一版拒绝所有 symlink；没有投影链接或环境内绝对链接语义。
- 删除重建同名环境被视为新环境（新 UUID、generation 1）；同 UUID generation 切换未实现。
- 迁移只针对 schema 1 旧记录；不自动迁移真实 `.lab/state`。

## 文件与文档

- 实现：`src/multilinux/{identity,registry,control,share_label,catalog,store,service,on_demand,cli,runtime}.py`
- 说明：`设计/Unifier-v2-第一阶段实现.md`
- 交接：`.lab/agents/unifier-v2-stage-a-20261023/finalize-handoff.md`
- 入口：`python3 tools/unifier.py`

## 冻结清单说明

`evidence/architecture-freeze-2026-10-23/SHA256SUMS` 本身没有被修改，历史 evidence 目录也没有被修改。由于本轮按任务要求必要地修改了 `README.md`、`HANDOFF.md` 和 `src/multilinux/` 下的源码，整仓 `sha256sum --check` 对这些**预期变更文件**不再通过；历史冻结材料仍按原样保留。

未提交、未推送。
