# Unifier v0.0.7 本机发布

**已部署。** 2026-11-14（北京时间），本机日用从 0.0.6 切换到 0.0.7。全局入口、FreeCAD 和会议各两条动态包装均返回 0.0.7，Dolphin 服务菜单切换到新发布目录中同字节 helper。日用业务状态及容器保持，既有会议 recovery-required 租约留存。

- 正式包：`/home/omg/.local/share/unifier-code/packages/0.0.7/unifier-0.0.7.tar.gz`
- SHA-256：`6d08eadb8142a29063cf9b9e236176ff8d8e3d496532dc31f756cbf15273cf8a`
- 代码目录：`/home/omg/.local/share/unifier-code/releases/0.0.7-6d08eadb8142`
- 验包：独立解包逐项核对 326 项清单，265 个 Python 文件、42 个别名及 5 条 CLI 入口。全仓共 1070 项，1065 通过、5 条件跳过。
- 来源：候选包 SHA-256 `7f99118f0a34d10df73b8f94624ce3119679f8fc3e11acb4d218f7aa5f1a4a2b`。326 项相同路径，只有版本常量和版本说明两处内容改变。候选已通过隔离 Debian 13 Zenity 交付和撤销实机。正式包通过离线验包和安装入口验证，本轮没有重新启动日用应用或客体。

[构建回执](build-t1/receipt.json) · [文件差异](promotion.json) · [部署台账](deployment.json) · [保全核对](final-verification.json)

## 回退

0.0.6 的完整代码、原包、入口和菜单状态保留。修正上一版本遗留的回退助手硬编码版本并重新验证。使用最终回退助手在私有副本实际完成了 0.0.6 入口与 Dolphin 菜单的精确恢复。第一次部署验证失败日志保留。日用入口目前指向 0.0.7。

先退出从 0.0.7 开启的应用，并完成相关租约、事务或撤销，再使用回退脚本。默认只检查当前入口、包清单和菜单是否匹配预期，加 `--apply` 才修改。

```bash
python3 /home/omg/.local/state/unifier/rollback/v007-20261114T155722+0800-jro94k45/restore.py --apply
```

[回退演练](rollback-copy-t1/command.log) · [首次回退预检问题](deployment-first-failure.json) · [修正后部署检查](deploy-verify-r2/receipt.json)

## 清理

将截至 0.0.4 的 23 项本机旧备份移入用户回收站，共约 54 MiB。含 12 个旧代码副本、6 个旧回退链接、4 套回退材料和 1 套 0.0.2 旧应用包装备份。回收站条目带原路径，可恢复。仓库追踪的 0.0.4 历史正式包、实机回执、应用数据、非版本夹具和 0.0.5/0.0.6 保留。主工作树只更新当前文档，版本常量继续为 0.0.7.dev0，原 Git HEAD 和索引保留。

[清理清单与恢复位置](cleanup.json) · [清理回执](cleanup-t1/receipt.json)
