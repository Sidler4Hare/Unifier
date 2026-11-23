# Fedora 44 本地 RPM 事务验收

2026-11-13，Ubuntu 原 Fedora generation 1 固定实例。使用既有 v0.0.7.dev0 验证副本及已与当前工作树核对摘要的正式恢复源码。执行使用 Unifier 正式包服务和原生 DNF，包签名配置沿用原实例。

| 用例 | 结果 | 事务 |
|---|---|---|
| 本地 RPM 1.0 安装 | PASS。DNF 退出 0，清单新增 1.0-1.noarch，版本文件为 1.0。 | txn959731f44839 |
| 本地 RPM 2.0 升级 | PASS。DNF 退出 0，清单仅保留 2.0-1.noarch，版本文件为 2.0。 | txn20d0d70d43fd |
| 删除 | PASS。DNF 退出 0，版本文件读取退出 1，原生清单恢复至原 154 项及原 SHA-256。 | txn4178e37d0bd3 |

同管理根 Arch 在三次事务前后均保持停止，140 项实库摘要与登记一致。六次只读清单存储挂载均已释放，最终存储日志为 released。Arch 可写层根目录的 ctime 发生变化，mtime、inode、原生包日志、历史事务、原中止回执、登记和租约摘要均一致。未对非目标逐文件做全量哈希。

远端 5 个完整 CID 及最终状态一致，新增 0。三次 Fedora start 均有 died 和 cleanup，14 次 exec 均有 exec_died。Fedora 最终 PID 0、无租约/桥、清单 verified，无恢复待办。远端本轮工作/监督单元、专用 slice 和目标 scope 已退出；本机上传保护单元已退出。Ubuntu 宿主包清单与桌面入口前后一致；本机 boot/KWin 身份、Git 索引、产品源码和日常安装入口核对一致。

实机作业峰值约 475 MiB、33 任务，客体采样峰值约 355 MiB，宿主最低可用约 112.4 GiB，PSI full/some 最高均 0.00%。无 OOM、硬内存或任务限额事件，监督及测试命令退出 0。两份 RPM、原生事务、事件和采样保存在本轮私有证据中。

本轮关闭 Fedora 普通本地 RPM 矩阵。签名拒绝、脚本失败及主动中断另有验收要求。Debian 本地包及 Arch 完整升级和剩余事务尚待完成。

[结构化核验](verification.json) · [原始证据](remote-evidence.tar) · [前置快照](remote-before.json) · [终态](remote-after.json) · [存储与包清单](terminal-storage.txt)
