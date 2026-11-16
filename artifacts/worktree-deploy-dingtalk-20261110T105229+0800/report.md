# 结果

日常入口切到脏工作树快照，钉钉已进入宿主软件列表。命令仍按设计以失败退出，因为三个运行库在 Trixie 确实没有仓库提供者。

## 三轮 install

| 轮次 | 事务 | 结论 |
|---|---|---|
| `txn515740660649`（改动前，10:14） | 补库一个都没装，phase=failed，`host_entries.added=[]` | 用户看到的“没进应用区” |
| `txn2820aa8cdd07`（10:53） | 补进 9 个包，缺库从 12 降到 4；走通登记分支，但 `add_app` 抛错被说成“不可执行” | 证明补库修复生效，暴露第二个缺陷 |
| `txnd18869294234`（11:04） | 缺库剩 3 个；登记 `com-alibabainc-dingtalk` 并写入菜单 | 钉钉出现在软件列表，退出码仍 1 |

现场暴露、随后修掉的第三个缺陷：`_app_id`/`_internal_app_id` 会产出带点的别名（桌面 ID `com.alibabainc.dingtalk`），记录库的 ID 规则只允许小写字母、数字和连字符，于是任何点号桌面 ID 的包都永远登记不上；同时 `install.entry_missing` 把 `add_app` 的任何异常都说成“在容器内不可执行”，JSON 里才有真原因。修法：发现阶段把别名折叠成连字符并保证首字符合规；人话消息带上 `{detail}`。回归用例 `test_dotted_desktop_id_is_registered_under_a_store_safe_alias`。

## 部署与回退

- 快照 r1 `releases/0.0.4-wt20261110-c947077f-dirty`、r2 `...-dirty-r2`（只含 bin/src/config/fixtures/tools，`DEPLOY-NOTE.md` 记来源与文件哈希；工作树不作日常入口）
- 现入口：r2；回退 `.unifier-8vd0uxms.rollback`→r1、`.unifier-1yb4yve8.rollback`→0.0.4-b04db4836d79
- 双击入口 `package-offer.json` 与 `unifier-package-offer.desktop` 已同步到 r2，见 `entry-switch.json`
- 未提交、未重新打发布包；已装发布包目录仍完整保留

## 边界

- 钉钉只是登记并可从菜单启动，没启动过：缺的 `liblber-2.4.so.2`/`libldap_r-2.4.so.2` 被自带 `libcurl.so.4` 需要，`libpangox-1.0.so.0` 被自带 gtkglext 需要，都不在主启动路径上，但这是推断不是验收
- 只唤醒并停止了 `ml-6b061a2f-meeting`；Arch 日常容器保持 46 小时前的停止状态；桌面会话未动
- 中途为定位原因临时加的 `probe-dingtalk` 登记已 `app remove` 删除，launches 目录恢复原有四条
- 全仓离线检查仍有 1 项与本改动无关的失败（agent bridge），见 HANDOFF
