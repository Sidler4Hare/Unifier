# Arch 重建结果

新容器 `ml-104d8e92-arch`（`e02fb4253a59`）已创建，状态是 created，pid 0。保护器 `info()` 准入通过。没有启动，没有执行 `pacman -Syu`。

限额：768 MiB 内存、896 MiB 含交换、1 CPU、192 任务、`unifier-tests.slice`、私有 IPC。镜像仍是 `archlinux@sha256:363129b67b2bc6c5a634f8fb4961a7e2eba213a2ec075d159127b1260290a722`。只挂了两个项目目录，没有 Wayland、X11、Pulse、PipeWire 或 xauth。

旧容器改名为 `ml-104d8e92-arch-20261103`（`ca04aa07e2aa`），保持 Exited，可写层没有复制。那一层约 2.4 GiB，超过本阶段 1 GiB 写入预算。环境记录 generation 改为 2，desktop 清空。记录里的包清单哈希仍描述旧容器的 296 个包，不是这台新根文件系统。

保护器：累计的 `memory.high` 和 75% 匿名内存不再截停。宿主保留 1 GiB、PSI、OOM、任务数，以及内存达到硬上限 90% 并连续两拍，仍然停止。桌面套接字即使写进规格也会拒绝。策略测试 11 项通过。

日常 FreeCAD、会议和固定 Debian 仍是 Exited。当前没有运行中的容器。
