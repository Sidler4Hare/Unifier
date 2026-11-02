# 宿主项目交给 Arch 保存

2026-10-23。报告：[跨应用保存原型](../../调研/跨应用保存原型-2026-10-23.md)。

使用已经装有 FreeCAD 的停止容器 `ml-edae1d18-complex-arch`。不安装包，不改挂载。样本写在该容器原有共享目录的 `work/cross-save-2026-10-23/`。原先 7 个证据文件只做哈希比较。

- `probe.py` 与 `result.json`：rename 保存、截断保存、只读文件、排好顺序的替换竞争、FreeCAD 保存和默认位置。其中描述符竞争在写入前失败，不作为结论。
- `probe_followup.py` 与 `result-followup.json`：修正后的旧描述符写入，以及 FCBak 和 STEP 的回读。

`probe.py` 需要新的 `--output`。`probe_followup.py` 在结果文件已存在时拒绝重跑。容器若不是停止状态，两个脚本都直接退出。启动所需的历史 Xauthority 路径若不存在，脚本会放一个空文件并在结束时删除。
