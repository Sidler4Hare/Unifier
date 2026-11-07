# 2026-11-07 正式安装与依赖用例

只使用固定 Debian CID `e58754f0645ae1ff2924cb09a7264cc821e44893e7589a7fd0ebca09ce5f5e45`。不新建容器，不改 Arch 配额，不操作活动桌面。

先经正式 `install_packages` 安装 gcc、binutils、dpkg-dev、apt-file。随后在客体内编译本轮小包，再用 `install_local` / `repair_dependencies` / `remove_packages` 跑 DEP01–DEP06、DEP08 和 SH09。DEP07 没有签名夹具仓库，DEP13 不在这台唯一容器上打断 dpkg，DEP11 只观察外部命令能力。下载预算沿用 200 MiB；已核对 Contents-amd64.gz 为 12698511 字节，工具链模拟下载约 66.5 MiB。

控制器仍是 384 MiB / 288 MiB / 128 任务。汇总组仍是 1 GiB。保护再触发即停止。
