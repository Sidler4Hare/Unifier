# D 受控源清理与完整移动真实验收

2026-10-25，dsh Command Code `deepseek/deepseek-v4.1-flash` 实现，主控独立探针、复核并验收。本机受限普通文件的受控完整移动已通过；默认 move 仍保留源。**304 项回归：300 通过、4 条件跳过。** 以 `acceptance-result.json` 及 `primary-final-*` 为最终源码证据，早期失败保留。

| 验收 | 结果 |
|---|---|
| 受控移动 | 35 项真实用例；Btrfs→tmpfs、tmpfs→Btrfs、同文件系统均完成 DONE，目标单链接/单登记、源对象删除、内部暂存与隔离目录清理 |
| 每阶段中断 | 16 个已暴露检查点真实 SIGKILL，随后恢复两次；包括 capture、隔离、删除 intent、source removed，均完成 |
| 日志窗口 | 7 项独立用例；重命名/删除效果发生后、日志写入前强杀，恢复本身连续强杀两次；未知同名隔离目录不认领或 chmod |
| 变化与冲突 | 已有 reader/writer、mmap、实际非阻塞租约中断、源/目标晚到硬链接、最终删除窗口策略撤销及根/目标替换均保留字节；原始名称被新文件占用时保留新对象 |
| 旧行为与空间不足 | 原有 20 项复制/保守移动真实用例、真实限额 tmpfs ENOSPC 与未完成 CLI exit 1 通过 |
| 公共 CLI | 新受控 move 的 7 条真实命令（exit 0、status、恢复两次、resolve），另有 9 条旧 CLI 兼容；中文/空格/单引号/分号/百分号按单参数保留 |
| 三发行版 | 新隔离 Debian/Fedora/Arch 的 C FUSE/缺失后端/生命周期回归及 C→D 宿主复制均通过，私有只读源不授予清理能力 |
| 保全与清理 | 原有 27 个容器及状态、23 个主状态文件、13,448 个此前证据文件及 B/C/D 校验清单保持不变；新建 3 个容器及卷已精确清理，无本轮 FUSE/storage mount 残留 |

## 使用

```bash
python3 tools/unifier.py root add /absolute/source --cleanup-policy controlled
python3 tools/unifier.py root add /absolute/target
python3 tools/unifier.py file move SOURCE_ROOT_UUID source.bin TARGET_ROOT_UUID target.bin --cleanup-source
python3 tools/unifier.py file status OPERATION_UUID
python3 tools/unifier.py file recover OPERATION_UUID
```

仅源根显式 controlled **且**本次 move 显式 `--cleanup-source` 才有删除权；源必须本地 read-write，不是 C 只读视图。旧待清理 journal 不会因升级或之后改变策略获得权限。默认 retained move 返回 SOURCE_CLEANUP_PENDING 和 exit 1；完整受控 move 为 DONE、complete=true、source_retained=false 和 exit 0。被重新占用的原始文件名属于新对象，不随原对象清理。

源独占文件租约阻止新的打开/截断并拒绝已有 FD/mmap；以无覆盖 rename 捕获到绑定 inode 的 0700 私有同文件系统隔离目录，再核对实际捕获对象。最终删除前重新核对源/目标授权、根/父目录/当前路径/哈希/链接数和唯一登记，同时持源和目标租约。只 unlink 已核对的私有捕获条目，不对未核对原路径执行删除。

租约不阻止 rename/unlink，参见 [Linux F_SETLEASE](https://man7.org/linux/man-pages/man2/F_SETLEASE.2const.html)；无覆盖捕获见 [renameat2](https://man7.org/linux/man-pages/man2/rename.2.html)。受控 namespace 假定同 UID 不恶意篡改私有控制状态，不宣称 kernel CAS、跨文件系统原子 rename 或跨系统原子事务。未支持目录递归、ACL/稀疏/设备/符号链接/硬链接源及完整元数据保真；不支持的租约/主线程条件保守保留源。未做宿主断电、两物理磁盘或跨宿主验收。

`mkdir` 已发生但其 inode 尚未落日志的中断窗口，现有同名目录的归属无法证明，会保留源并进入 NEEDS_RECONCILIATION；不会认领或删除未知目录。其余已证明捕获/删除进度可幂等恢复。进程强杀结果不能代替断电持久性证明。

## 复跑与恢复

`probe_cleanup_native.py`、`probe_cleanup_windows.py`、`probe_cleanup_cli.py` 为主控探针；`run_acceptance.py --prefix NEW` 组合验证当前不变源码。三个测试容器已清理，复跑三发行版须先在新目录准备独立 fixtures，不能把旧 CID 当作活动实例。实现说明见 `设计/Unifier-v2-D受控移动实现.md`。D 通过后才派发 E；本报告不扩展为 E/F 验收。

D 检查点备份及 SHA256 见 `.lab/agents/unifier-v2-stage-d2-20261025/completed.json`；包含代码/测试/配置/设计和 A/B/C/D 证据，排除私有运行态、凭据、缓存。未提交或推送。
