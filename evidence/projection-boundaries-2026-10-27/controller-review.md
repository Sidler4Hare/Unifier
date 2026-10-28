# 主控中间审查

所有中间源码/失败保留，不以 dsh 完成或普通 unittest 代替真实挂载。

- attempt1 的有限 LRU inode 表在容量淘汰后重新编号。小预算 4、64 次其他对象 churn 后原条目 2→67；`identity-attempt1.json` 失败。主控暂停 dsh 并给出 `controller-feedback.md`，同一会话继续修订。
- attempt2 已改为纯身份哈希：相同小预算反例通过，nodes 和 inode_ids 均回到 1。真实挂载原三反例、无显式 fstat、mtime 保留的 mmap、逐组件错误、根 clamp 与目录释放 churn 均通过。
- attempt2 活跃碰撞注入正确拒绝 EOVERFLOW，但 10 次失败 FD 从 7→17；分配入口在 `_inode_for` 异常时未回滚 anchor。见 `collision-attempt2.json`。
- attempt2 新链接解析在缺失中间组件时保留带 `..` 的未解析后缀。先获取投影 link 文本、随后创建前缀目录，保存的路径可越出投影到测试宿主 sentinel。见 `kernel-attempt2/result.json` 的 `dangling-suffix-confinement`。最终版必须拒绝这种歧义后缀或以等价安全方案处理，不得直接拼接。
- `_attr_state` 独立字典只 prune 被释放的目录 node，未覆盖“子节点已 FORGET、仍由目录 snapshot 内部引用持有，随后 RELEASEDIR 批量回收”路径。最终应把属性版本放在可回收 Node 上，或确保所有收集路径清理；不得增加无界历史缓存。
- 页失效不能把通知写入失败当作成功并更新版本；应保留待重试版本、明确失败，后续成功重验证仍发送失效通知。真实 mtime/ctime 不伪造。

## 最终处理

- 稳定身份映射、小预算 churn 与碰撞回滚均通过；碰撞分支在施工接管快照中已关闭候选 anchor，最终注入 FD 7→7。
- 主控将缺失/非目录前缀后的未解析 `..` 改为明确错误；不接受删除组件后伪造新目标。宿主 sentinel 的两种真实路径均未越界。
- 属性版本移入 Node，RELEASEDIR 最后内部引用收集时自然释放；新增失败通知重试与失败 LOOKUP 引用回滚测试。
- 首轮最终挂载暴露内核 inode absence 通知 ENOENT 被传播为路径失败，已核对上游内核语义并单独处理；其他通知错误保留明确失败。旧失败源码/结果不改写，最终 v2 的 22 核心、9 边界及三客体78项全部通过。
- 默认挂载点首次正常撤销4/5、最终清理5/5，外部ChatGPT持有者现场保留，不能宣布即时撤销问题关闭。
