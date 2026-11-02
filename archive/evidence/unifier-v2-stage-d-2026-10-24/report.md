# D 首个受管文件闭环真实验收

C 通过后，继续由 dsh Command Code `deepseek/deepseek-v4.1-flash` 实现 D，主控独立实测、修复并复核。**D 的受限普通文件复制/保守恢复闭环已通过本机验证；没有完成自动源删除或原子跨文件系统移动。** 当前完整回归 **293 项：289 通过、4 项原有条件跳过**。实现与证据均未提交/推送。

| 验收 | 结果与证据 |
|---|---|
| Btrfs → tmpfs | 20 个真实用例全部通过，`native-final.json`；完成 copy 后目标单链接，现有 FileControl 可正常观察 |
| 持久阶段进程退出 | INTENT、STAGING、stage 创建/完成、发布前日志窗口、TARGET_PUBLISHED、注册提交前日志窗口、REGISTRY_COMMITTED、stage 清理、DONE、SOURCE_CLEANUP_PENDING 均以子进程无清理退出后恢复两次，无重复登记 |
| 冲突/替换 | 目标占用、同内容无关目标、stage 替换、源变化、目标根替换、发布目标替换、只读目标均保留原有字节或拒绝，无覆盖认领 |
| 真实 ENOSPC | 隔离 user/mount namespace 的 256 KiB tmpfs，复制 2 MiB 随机源触发内核 ENOSPC；源/可解释暂存保留，无发布目标，无登记，状态 needs-reconciliation，正常卸载，`enospc-final2.json` |
| 三发行版私有文件 → 宿主 | Debian/Fedora/Arch 的停止环境通过 C 只读投影登记源根并复制；保持环境绑定，D 持 FD 时 C 拒绝撤销且仍可读；view 撤销后假空目录不被当作源，`view-copy-final.json` |
| 公共 CLI | 9 条真实命令，中文/空格/单引号/分号作为单参数保存；copy 完成 exit 0，未完成 move exit 1，目标占用 exit 1；status/recover/resolve 通过，`cli-final.json` |
| C 回归 | D 最终源码重新通过三发行版 FUSE、busy/worker-kill/broker-kill/外部引用以及后端缺失测试，`c-regression-gate.json` |
| 日常 FreeCAD | 保留的用户菜单更新为已验证代码，配置/状态/入口未重建；再次两次实际菜单启动、正常关闭、自动停止，同一 CID，`daily-source-upgrade.json`、`freecad-final/menu-validation.json` |
| 清理与保全 | 3 个测试容器和卷已删除；最终原有 26 个加 1 个日常 FreeCAD，本轮 Unifier FUSE/storage mounts 空；主项目 23 个状态文件、12,670 历史文件、B/C 清单均核对不变，`postconditions.json` |

`acceptance-result.json` 保存最终源码 SHA256 和组合验证结果。`verify_evidence.py` 可离线复核当前代码、各报告和保留的桌面代码快照。早期 SourceView 校验的未定义 backend 变量已在真实测试中修复；CLI copy/move 仅在 complete=true 时返回 0，避免 ENOSPC/部分移动被脚本当成完整成功。失败记录保留。

入口示例：

```bash
python3 tools/unifier.py root add /absolute/source --policy read-only
python3 tools/unifier.py root add /absolute/destination
python3 tools/unifier.py file copy SOURCE_ROOT_UUID source.txt TARGET_ROOT_UUID target.txt
python3 tools/unifier.py file move SOURCE_ROOT_UUID source.txt TARGET_ROOT_UUID target.txt
python3 tools/unifier.py file status OPERATION_UUID
python3 tools/unifier.py file recover OPERATION_UUID
```

`move` 当前发布并登记目标后返回 `SOURCE_CLEANUP_PENDING`，源保留，CLI exit 1 表示完整移动未完成；API 的 copy_complete=true 与 complete=false 分开。不会自动删除不受控源。目标必须未占用；普通文件范围内仍明确拒绝目录、ACL、稀疏/压缩迹象、设备、symlink 和硬链接源，未承诺全元数据保真、对外部写入者的 CAS 或宿主断电恢复。跨文件系统证据为本机 Btrfs/tmpfs，tmpfs 不提供断电持久性，未做两物理磁盘、压力或跨宿主验收。E/F 尚未开始。

CAD 原先只有测试登记，测试环境随后被清理，因此没有菜单入口。本轮保留 `/home/omg/.local/share/applications/unifier-freecad.desktop`，名称 **FreeCAD**，工作目录 `/home/omg/文档/Unifier-CAD`。入口使用独立保留的 `/home/omg/.local/share/multilinux/apps/freecad`，已有代码备份；不依赖已清理的验收容器。此次验证的是菜单启动和生命周期；通用 Save As 默认位置仍沿用 B 的有限边界。

重跑请使用新输出名和新隔离工作目录。`probe_native.py --output NEW.json`、`probe_enospc.py --output NEW.json` 可直接运行；`probe_cli.py --workdir NEW_DIRECTORY --output NEW.json` 使用全新 CLI 快照。原 C 测试容器已清理，如需三发行版复验，将 C 的 `prepare_containers.py` 复制到仓库内新的证据子目录并运行，生成独立 fixtures 后执行 `probe_view_copy.py --fixtures NEW/fixtures.json --output NEW/result.json`；结束后用 `cleanup_fixtures.py --fixtures NEW/fixtures.json --output NEW/cleanup.json` 仅清理其精确绑定实例。不要重写旧 evidence 或把旧已删除 CID 当作活动环境。

最终一致性复核见 `evidence-audit.json`：当前 29 个源码文件、CLI/日常应用副本与最终 gate 哈希一致，B/C 清单全部通过；现场复核原容器状态、主状态、历史文件和本轮挂载均通过。该复核是证据一致性检查，不是额外的产品测试。系统 `fuse.portal` 属于文档门户，未清理或算作本项目残留。

```bash
python3 evidence/unifier-v2-stage-d-2026-10-24/verify_evidence.py --output NEW.json
# 当前宿主另核对容器、挂载及私有原始基线；不启动环境或重放探针
python3 evidence/unifier-v2-stage-d-2026-10-24/verify_evidence.py --live-checks --output ANOTHER_NEW.json
```

本轮代码、测试、配置、设计说明与 A/B/C/D 证据保留于 `.lab/agents/unifier-v2-stage-d-20261024/backup/stage-d-accepted.tar.gz`，哈希与 dsh 会话信息见同目录上级的 `completed.json`。备份排除 `.lab`、凭据、运行时套接字和缓存；C 的独立接受备份继续保留。
