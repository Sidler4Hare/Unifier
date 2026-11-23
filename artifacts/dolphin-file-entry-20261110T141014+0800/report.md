# 启动包装、安装登记与 Dolphin 文件入口

## 已部署

- 本机日常入口已从 v0.0.4 r2 切到 r3 快照。Debian 和 Arch 两套应用目录中的四份 `ml.py`、`unifier.py` 包装以及安装包入口跟随 `~/.local/bin/unifier`。正在运行的应用进程沿用启动时的版本。
- 仅当事务以补库未收敛告终、包清单核验完毕、无需恢复且隔离检查无异常时，安装才继续登记入口。命令保留失败退出码。
- `dirs` 对照实际 CID 的 bind，显示宿主和客体路径、可写性和缺失状态。`path --files` 只输出核验过的可用共享目录。右键复制额外要求目标可写。
- Dolphin 已安装「Unifier · Debian 共享文件」位置和「复制到 Unifier Debian 共享目录」动作。复制适用于本地普通文件，按文件完成后发布，同名拒绝覆盖。Arch 的 `~/文档/Unifier-CAD` 在当前宿主缺失。

## 证据与回退

- `offline/` 首轮未达到连续 30 秒低压力准入，工作进程未启动。`offline-retry/` 已准入，暴露了模拟客体身份缺项和缺失环境的提示回归。修正后 `offline-final/` 的 60 项定向用例和复制夹具退出 0。`offline-copy-final/` 另外覆盖目标路径尾部换行，退出 0。
- `deploy.log` 记录切换。切换后核对了发布快照的文件字节、Dolphin 配置和回退备份哈希。 `cid-before.txt` 与 `cid-after.txt` 完全相同。此次没有运行包事务、启动容器或操作活动桌面。
- 尚无本轮 Dolphin 点选、客体读取附件或保存重开的真实工作流证据。
- 旧发布快照及原入口文件保留。回退入口为 `~/.local/state/unifier/rollback/dolphin-file-entry-20261110T141014+0800/restore.py`。回退会跳过部署后另行改动的文件。
