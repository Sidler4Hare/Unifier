# 2026-11-07 共享 CLI 一次复试

只跑原固定 Debian CID 上的 SH01–SH08。不重跑已经结束的停止态包库阶段，不提高控制器，不新建容器，不改 Arch 配额，不启动 GUI 或活动桌面。

上一轮 `shared-cli-envfix/` 记 `ABORTED_PSI`。当时汇总峰值约 385 MiB，宿主 MemAvailable 仍约 4.8 GiB，full avg10 升到 12.68。那次样本不能当作产品失败。随后控制器对自身 `memory.high` 事件改为立即停止，避免子组回收拖成宿主 PSI。本轮是这一保护类的一次有依据复试。再次触发 PSI、回收压力或其他保护就停止，不再自动重试。

控制器沿用 2026-11-06 已授权的 384 MiB / 288 MiB / 128 任务，不使用更高档。汇总组仍是 1 GiB RAM、128 MiB swap、256 任务、1 CPU。监督器独立 64 MiB / 16 任务。宿主保留线 1 GiB。调用后缀 `_r3`，复用已有 `accept1106-*` 注册，不改启动参数。

允许对象：CID `e58754f0645ae1ff2924cb09a7264cc821e44893e7589a7fd0ebca09ce5f5e45`，环境 `debian13`，UUID `ae147c3e-3e65-41ac-ac8d-9257c9202696`，generation 1。日常容器和其他测试容器保持停止。
