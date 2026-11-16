# 历史测试材料退役（2026-11-11）

旧 archive/evidence 的已提交探针、产物和复制的工程树已从当前工作树/索引移除。现行 src/tests/tools/fixtures、archive/notes 与验收摘要保留。残留未跟踪的历史容器状态/锁不是测试垃圾，本次不删除。

旧证据仍存在于清理前 Git 历史：主工作树 c947077f，Portal 工作树 f35ffe99。历史文档中的 evidence 链接是当时证据定位，不保证当前目录仍提供文件；run_archived_probe.py 需先恢复所需旧 driver/fixture，不能直接照旧文档运行。

单文件可用 `git show <上述提交>:archive/evidence/<轮次>/<文件>` 查看；整轮可用 git archive 导出到仓库外的私有目录。不要为日常测试恢复整棵历史树，不要恢复到运行中的容器管理目录。本次没有改写 Git 历史或清理对象库。

逐文件清理清单位于本机 `~/.local/state/unifier/maintenance/cleanup-20261111/`，包含恢复提交与删除前大小；最近 Portal/架构修复的原始回执保留在主工作树 artifacts 中。
