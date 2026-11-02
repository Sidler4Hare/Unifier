# Unifier v2 第一阶段 A：实现与验收说明

日期：2026-10-23 初版，2026-10-24 续修，Asia/Shanghai。分支：`codex/unifier-v2-control`，基线 `9a86520`。状态：**第一阶段 A 的可运行最小实现已接入，并完成普通共享目录标签政策的 B 关口修正；B–F 其余业务未完成。**

本阶段实现版本化环境身份与显式旧记录迁移、新应用启动绑定、独立数据根注册表、只读 `FileRef` 解析，以及普通共享目录的显式 SELinux 标签政策。没有实现文件投影、FUSE、复制/移动、文件处理器执行、daemon IPC 或 UI。真实 Debian/Fedora/Arch 容器验证由监督者现场执行并单独报告；本文件只记录代码与离线回归结论。

2026-10-24 续修针对独立复审反例：拒绝 schema 1/2 的 null 身份；迁移前校验完整旧记录字段与 runtime 类型；备份按源 SHA-256 命名；显式父根引用不得越过嵌套子根政策；拒绝 `nlink>1` 的普通文件；用已打开 FD 的 `mnt_id` 检测同设备 bind mount；畸形注册表字段返回 `MultilinuxError`。

## 1. 版本化环境身份

环境记录新增：

- `environment_uuid`：新记录创建时生成的规范小写 UUID（版本 4）。
- `generation`：正整数；本轮新建记录从 `1` 开始。
- `schema_version`：新记录为 `2`；历史记录 `1` 视为无身份的旧记录。

用户可见的 `id` 仍是别名。`content_revision`、`container_id`、包清单路径和既有字段保留。`env list`、`env inspect`、`env plan` 输出携带 `environment_uuid` 与 `generation`；没有身份的旧记录显示 `null`，**只读操作不会补写身份**。

### 显式迁移

```bash
python3 tools/unifier.py env migrate ID
```

语义：

1. 只接受 schema 1（或缺少 schema 版本，按 1 处理）、且 `environment_uuid` 与 `generation` 两个键**完全不存在**的有效旧记录。
2. 先通过现有 `resolve_record_contract()` 校验目标/系统契约，并校验记录 `id` 与文件名一致。
3. 再用迁移专用校验检查必要字段：`runtime.container_id/container_name/project`、`writable_revision` 与容器实例一致、基础镜像 digest/locked reference、包清单摘要、`shared_mounts`、`observed`、创建者字段等。只有 `id`/`target` 的伪旧记录不能迁移。后续代码新增的可选 runtime/desktop/arch 字段仍保持可选，以兼容原有合法旧记录。
4. 缺失身份与已有身份都不重新生成；已有身份返回 `already-migrated`，不写记录。
5. 显式 `environment_uuid=null` 或 `generation=null`（一个或两个）属于畸形身份，直接失败；半身份（只有 UUID 或只有 generation）、非法 UUID/generation、schema 2 缺任意身份、未来 schema、broken contract、不可读 JSON 也全部失败且保持原字节。
6. 迁移前按**本次源字节的 SHA-256** 保存到
   `.lab/state/environments/backups/ID.pre-identity-<sha256>.json`。同一源重试稳定复用；同名环境删除后导入另一份旧记录会生成不同 digest 的独立备份，两个原件都可恢复，不会覆盖。
7. 每记录使用 `.lab/state/environments/.identity-locks/ID.lock` 的 `flock`。多个进程并发迁移时只有一个写入，其余读到同一 UUID；写入使用临时文件 `fsync`、原子 rename 和父目录 `fsync`。
8. 迁移路径完全不访问 runtime，不启动、停止、创建容器，不改 `container_id`、包清单或内容修订。

删除环境后同名再建会被视为**新的环境**：新的 UUID，generation 仍从 1 开始。旧的 `(environment_uuid, generation, container_id)` 绑定因此拒绝。保留同一 UUID 的代际切换/重建编排未在本阶段实现。

## 2. 应用启动绑定

新 launch 记录为 schema 2，包含：

```json
"environment_binding": {
  "environment_id": "debian13",
  "environment_uuid": "…",
  "generation": 1,
  "container_id": "…"
}
```

launch 顶层也镜像 `environment_uuid`、`generation`、`container_id`；校验会拒绝顶层与绑定不一致的记录。执行前核对发生在：

- `EnvironmentService.exec_app()`：先只读核对绑定，再调用 `start()`；`start()` 在生命周期锁内、任何 runtime 变更前再次核对记录与当前容器实例。
- `on_demand.supervised_open()`：启动 detached worker 前做一次只读预检。
- `on_demand.managed_open()` 与 `LaunchGroup.acquire()`：在创建 bridge、取得租约或调用 `start()` 之前核对；`start()` 仍带 `expected_binding` 复核。
- `service.start()` 可选 `expected_binding`：锁内拒绝 UUID/generation/container_id 不匹配，或同名容器实例已变化。

因此删除并重建同名环境后，旧应用不会启动新容器，也不会把旧授权自动授予新容器。

旧 launch 记录（schema 1、无 `environment_binding`）仍可 `app show` 读取，也仍可删除/清理/取消导出；但 `app open`、`app exec`、`app export` 与 on-demand 监督路径全部失败，并提示显式重新登记（`app remove` 后重新 `app add`）。open/exec 在 runtime 变更前拒绝。没有隐式按同名环境补绑定，也没有改旧 launch 数据来“迁移授权”。

## 3. 独立数据根

新增 `src/multilinux/registry.py`、`src/multilinux/control.py`。登记表在：

```text
.lab/state/data-roots/registry.json
.lab/state/data-roots/registry.lock
```

记录字段：

| 字段 | 本轮值 |
|---|---|
| `root_uuid` | 规范 UUID |
| `management_domain` | 仅 `host-project` |
| `backend` | 仅 `local-directory` |
| `locator` | 规范化绝对宿主目录 |
| `access_policy` | `read-only` 或 `read-write` |
| `schema_version` | `1` |
| `root_dev` / `root_ino` | 注册时观测，用于解析时检测根替换 |
| `created_at` | 时间戳 |

- 注册路径必须是已存在的目录；`root add` 只登记，不挂载、不复制、不改 SELinux 标签。
- 未知 backend、管理域或访问政策直接拒绝。
- 同一真实路径、符号链接别名或相同 `(st_dev, st_ino)` 的根保守拒绝，并报告已存在的 `root_uuid`。
- 允许显式嵌套根。路径选择按组件比较，最长根优先；不是裸字符串前缀授权。真正打开文件时仍用锚定 FD 逐段校验。
- **嵌套政策一致性：** 用父根 UUID 显式解析进入已登记子根的路径会被拒绝，并要求改用子根 UUID。这样不会出现 `--path` 选子根只读、父根显式引用却返回父根读写的矛盾。先前已生成的父根 `FileRef` 保持其历史观测，不会自动改写。
- 持久注册表字段会先做类型检查；`backend`/`management_domain`/`access_policy` 为 list、dict、None 等畸形值时返回 `MultilinuxError`，不会抛 `TypeError`，也不改写源注册表。
- `root remove` 只删除登记，**绝不删除目录**。数据根不存放在 `EnvironmentStore` 中，删除环境不会删除数据根。

```bash
python3 tools/unifier.py root add /path/to/project --policy read-write
python3 tools/unifier.py root list
python3 tools/unifier.py root show ROOT_UUID
python3 tools/unifier.py root match /path/to/project/file.txt
python3 tools/unifier.py root remove ROOT_UUID
```

JSON 存储使用进程间 `flock`、临时文件、`fsync`、原子 rename 和父目录 `fsync`。

## 4. `FileRef` 与只读文件解析

```bash
python3 tools/unifier.py file resolve ROOT_UUID relative/path.txt --role document
python3 tools/unifier.py file resolve --path /path/to/project/file.txt --role project
```

返回包含：

- `root_uuid`、`relative_path`、`role`、`management_domain`、`backend`、`access_policy`；
- `observed_version`：`dev`、`ino`、`mode`、`nlink`、`uid`、`gid`、`size`、`mtime_ns`、`ctime_ns`、`file_type`；
- 显式能力边界：`observe=true`；`read`、`write`、`read_content`、`write_content`、`create`、`delete`、`copy`、`move`、`execute` 全部 `false`；
- `implemented_operations=["observe"]`；
- `execution.interpreter=null`、`execution.environment=null`。

`role` 只是文件角色，不选择解释器、编程语言或环境。管理域也不等于执行环境。本轮不执行文件、不做 MIME/PATH 路由、不 fallback。

### 解析边界

- 相对路径不得为空、不得绝对、不得含空组件、`.` 或 `..`。
- 用注册根目录的锚定 FD 开始，对每一段使用 `O_PATH | O_NOFOLLOW`；中间目录还要求 `O_DIRECTORY`。
- 第一版**拒绝所有符号链接**（中间段和最终段都拒绝），因此也不跟随 magic link。
- 最终 FD 必须指向普通文件；对最终 FD 做 `fstat`，用该观测构建 `FileRef`。
- 用已打开 FD 的 `/proc/self/fdinfo/<fd>` `mnt_id` 比较根、中间目录和最终对象；同设备 bind mount 也会因 `mnt_id` 不同被拒绝。`mnt_id` 不可读或缺失时明确拒绝该解析能力，不静默退回 `st_dev` 检查。
- 仍保留 `st_dev` 检查作为额外防线；最终对象与根目录设备不一致时拒绝。
- 普通文件 `nlink > 1` 在未实现跨根硬链接别名核对前保守拒绝，不把同一 inode 的两个入口当作两个独立授权对象；`nlink == 1` 的普通文件不受影响。
- 打开前比较根路径的 `(st_dev, st_ino)` 与登记值；解析结束前再次 `stat` 根路径，检测根路径替换。竞态窗口内仍可能拿到旧根 FD，但不会因此跟随到新路径；这是检测而不是全局文件系统保证。
- 没有使用“先 `Path.resolve()` 再按原字符串 open”的检查/使用模式。
- 不声称限制同容器其他应用，也不声称解决任意外部写入竞态。

## 5. 普通共享目录的标签政策

普通共享目录不再默认添加 `:z`。新创建环境默认 `preserve-host`：

- Podman 挂载 argv 为 `host:container`，不加 `:z`/`:Z`；
- 不改变宿主目录的 SELinux 标签；
- 不会为了接通访问而自动设置 `label=disable`。

只有显式 `--share-label shared-relabel`（或 `EnvironmentService.create(share_label_policy="shared-relabel")`）才给普通共享目录添加 `:z`。该选择持久记录在：

- `runtime.share_label_policy`；
- 每个普通共享目录的 `shared_mounts[].options`（`""` 或 `"z"`）。

`CreateSpec`、CLI `env create`/`env plan`、持久记录和 `catalog` 冻结更换计划使用同一政策。把政策从 `preserve-host` 改为 `shared-relabel`（或反向）在 `env plan` 中显示为 `replace`，必须新建环境；现有容器和记录不会原地改挂载。

现有 desktop 环境继续使用已有的 `selinux_label=disable` 机制（`--desktop` 的会话 socket 需要），不修改宿主 SELinux 状态或全局设置。显式共享重标记与 desktop 标签是两件事，互不替代。

旧记录缺少 `runtime.share_label_policy` 时，根据 `shared_mounts[].options` 推断历史政策：含 `z`/`Z` 视为 `shared-relabel`，否则 `preserve-host`。`inspect`/`plan` 只显示有效政策，不写回；`env migrate` 也不添加该字段。持久政策与挂载 options 矛盾时明确报错。

在 enforcing 宿主上，`preserve-host` 可能被拒绝访问。此时错误应如实指向挂载/SELinux 拒绝；不得偷偷 fallback 到 relabel 或 `label=disable`。更细的 SELinux 政策、宿主适配和真实三发行版容器验收仍由后续现场验证覆盖。

## 6. 共享工作目录修复

`_shared_workdir()` 现在：

- 拒绝含 `..` 的 workdir；
- 按共享根的最长组件匹配选择根；
- 要求 workdir 是已存在目录；
- 将 workdir 与共享根都做真实路径解析，要求 workdir 仍在共享根内，拒绝共享内符号链接跳到宿主其他位置。

这是**登记时**校验，不能限制环境进程后续自己打开其他路径。

## 7. 测试与复现


```bash
python3 -m unittest discover -s tests -v
python3 tools/unifier.py --help
python3 tools/unifier.py env --help
python3 tools/unifier.py root --help
python3 tools/unifier.py file resolve --help
```

本轮最终回归：**203 项，199 通过，4 项按条件跳过**（现场集成测试条件未满足）。新增测试覆盖：

- 新记录 UUID/generation、只读视图不写盘；
- 迁移幂等、源 SHA-256 备份、并发多进程同一 UUID、同名不同旧记录保留两份原件；
- null/半身份、非法 UUID/generation、schema 2 缺身份、未来 schema、broken contract、缺失 runtime/包字段、runtime 枚举 list/dict/None 零写入且零 runtime；
- 同名重建后旧绑定在 `exec_app`、`managed_open`、`supervised_open`、`LaunchGroup.acquire` 前拒绝；
- 未迁移的旧环境不能新增应用；旧 launch 记录显式重新登记；
- 数据根与环境删除独立、重复/别名/嵌套根、未知政策；持久注册表 list/dict/None 畸形字段返回 `MultilinuxError` 且不改源注册表；
- `..`、绝对路径、空组件、符号链接、跨设备、同设备 bind mount、根替换；
- 父子根不同政策时显式父根引用拒绝、自动选择子根；旧父根引用保持历史；
- 跨根硬链接 `nlink>1` 拒绝，普通 `nlink=1` 文件通过；
- 编辑器原子替换后产生新的 `observed_version`；
- 普通共享目录默认 `preserve-host` 无 `:z`、显式 `shared-relabel` 才 `:z`、未知政策零 runtime、旧记录从 options 推断且不写回、政策变化计划为 replace；
- 共享 workdir 的 `..` 与符号链接逃逸。

所有代码回归使用临时目录和 `FakeRuntime`，没有读取或改写仓库现有 `.lab/state` 记录、既有容器、桌面入口或图形会话。同设备 bind mount 另外用监督者提供的真实私有 mount namespace 探针复验：`same_device=true, refused=true`，输出见 `evidence/unifier-v2-stage-a-2026-10-23/mount-boundary-probe.txt`。真实 Debian/Fedora/Arch 容器验证由监督者现场执行并单独报告；本文件不把离线回归替代为现场结论。

## 8. 未完成边界

- **B–F 均未完成。** 没有真实 GUI/宿主项目闭环验收，没有停止环境只读投影，没有受管复制/移动/源清理，没有项目会话/桌面入口撤销，没有压力与跨宿主验收。
- 没有 FUSE、合并根、卷覆盖视图、`/user/<distro>` 管理投影。
- 没有 `FileRef` 的读取内容、写入、复制、移动、删除、处理器执行、daemon IPC 或 UI。
- 没有同 UUID 的 generation 切换、环境自动迁移、回滚编排。
- 普通共享目录的默认 `:z` 重标记缺口已由显式 `preserve-host`/`shared-relabel` 政策关闭；更细的 SELinux 政策、宿主适配和 enforcing 现场矩阵仍未实现，新 root/file 入口不挂载、不 relabel。
- 没有解决外部写入竞态、并发防覆盖、断电/重启恢复。
- 真实 Debian/Fedora/Arch 容器验证待监督者现场执行并单独报告；本轮代码回归使用临时目录和 `FakeRuntime`。

## 9. 验收证据

`evidence/unifier-v2-stage-a-2026-10-23/` 保存本轮报告、`finalize-regression.txt`、`finalize-cli-help.txt` 和同设备 bind mount 探针输出。`report-2026-10-23.md`、`regression.txt` 和 `cli-help.txt` 保留为 2026-10-23 先前快照；主报告 `report.md` 说明其被 finalize 输出版本取代。历史 evidence、冻结清单、原实验报告和备份未修改。
