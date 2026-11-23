# Arch 门禁 2026-11-09

实机 AR03、AR01、AR02 未启动。固定测试容器 `ml-104d8e92-arch`（`ca04aa07e2aa`）保持 Exited (143)。`tools/reuse_test_guard.py info()` 在启动前拒绝：`fixed container limits/isolation are not admitted`。

实测 HostConfig：内存 0、交换 0、CPU 0、任务上限 2048、cgroup 父级 `user.slice`、IPC `host`。挂载包含当前桌面：`/run/user/1000/wayland-0`、`/tmp/.X11-unix/X0`、`/run/user/1000/pulse/native`、`/run/user/1000/pipewire-0`、`/run/user/1000/xauth_dnPjwq`。

未做：`podman update`、`podman start`、新建容器、改用 `ml-45dc9c7d-deps-arch` 或 `ml-06a72096-deps-arch`。这两台同样没有内存/CPU 上限，保持 Exited。日常容器 `ml-53c5c8a5-freecad-arch`、`ml-6b061a2f-meeting` 和 Debian `ml-2f4a1877-debian13` 保持 Exited。

离线契约通过，不记为 AR03–AR01 通过。`tests/test_packages.py` 与 `tests/test_package_inventory.py` 共 27 项，0.504 秒，退出码 0。监督峰值内存 35418112 字节，cgroup `memory.peak` 43106304 字节，任务峰值 3，无 OOM。范围是 fake runtime，`MULTILINUX_INTEGRATION=0`。没有估算真实 `pacman -Syu` 下载量，也没有读停机 pacman 清单。

证据：`live-blocked.json`、`offline/receipt.json`、`offline/command.log`。作业单元 `unifier-arch-offline-guard` 与 `unifier-arch-offline-work` 结束后为 inactive。
