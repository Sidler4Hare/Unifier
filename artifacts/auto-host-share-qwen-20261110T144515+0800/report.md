# 自动共享首批编码
- 工作树分支为 `codex/auto-host-share`，基于 `c947077f`。复制最新设计和项目约束，原工作树已有改动留在原处，索引登记摘要核对一致。
- 通过 dsh 核验 `local-server / swift-1.5-iq3_s / max`，本地 Qwen 写入 `src/unifier/host_share_policy.py`。模型作业到时限时工作单元退出，保护器在进程身份重查时报告 procfs 条目消失。原回执状态 INVALID，进程组已清理。Qwen 未写出单测。
- 主控复核并整理模块，补齐 `tests/test_host_share_policy.py`。独立 T1 共 7 项通过，退出码 0。覆盖范围和模式、黑名单后代、相邻前缀、嵌套管理员目录、优先级、无效路径和配置不可变。
- 本批是纯路径决策。真实路径验证、FUSE、容器和 Dolphin 尚未接入，本轮未部署。
