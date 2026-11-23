# 验收材料

本目录以运行 ID 关联结果和证据。Git 保存经过审核的报告和少量校验汇总。新运行目录默认忽略，摘要审核后用 `git add -f -- <确切路径>` 逐文件暂存。

`tests/test_hardware_guard.py` 使用 AMD Fedora 44 离线包验收的六个固定输入，原路径与原字节作为测试 fixture 纳入 Git，以便在独立暂存快照内核对摘要和拒绝条件。

原始日志、解包源码、编译产物、容器状态和恢复资料由运行主机保管。报告中更细的证据链接可能指向这些本地材料。已从仓库索引退役的历史材料见 [恢复说明](../archive/README.md)。

2026-11-23 整理保留 11 月 3 日 soak 回执的 `result.json`。旧路径 `interrupted-controller-snapshot.json` 的原字节与该文件一致，Git blob 为 `b445a9e894068c6f96966992ad03e66b36041e59`。历史快照或按旧路径重建时可从 `bfa5fc9d86c79ca38eaff0ed904be9080bcee2a5` 取回。
