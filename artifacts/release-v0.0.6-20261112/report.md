# v0.0.6 WPS退出修复补丁部署

**本机部署完成。** 日常全局、FreeCAD和会议工具包装入口均返回 `Unifier 0.0.6`。旧v0.0.5代码和发布包保留，条件回滚预检通过。未提交、打tag、推送或发布远端。

## 发布范围和验证

基于已安装且290项清单完全匹配的v0.0.5，合入本轮5个产品文件的精确增量，另外更新版本、3个测试文件和说明。12个文件发生变化，其余正式包文件保持。没有直接用较旧的开发工作树覆盖安装版。三处重叠人工解决：保留 `--headless`、Portal准备后的env、launch_argv及启动前错误证明，另加退出策略。初次自动合并冲突回执保留，不是资源中止。候选差异见[release.patch](release.patch)。

- 发布包独立解包：943项，938通过、5条件跳过，退出0。
- 安装版另外6项检查全部通过：真实FD3/客体监督微型进程、精确辅助收尾、并行执行保留、策略注册与handler指纹；模块路径全部来自新安装目录。
- 全局、FreeCAD和会议包装入口均为0.0.6；`--exit-policy`、`--headless`和GTK Native Portal选项均存在。
- WPS真实工作流证据沿用修复阶段的SH10：两轮保存/重开及三次关闭，无KILL。此次部署不另开GUI或容器，不称安装版做过新的真实应用验收。

旧应用默认仍为wait；新策略必须显式登记 `--on-demand --exit-policy stop-children`，仅用于会等待界面结束的前台命令。本次检查的日常管理根没有WPS登记，未对其他应用批量改策略。已有运行中的代码不会因入口切换而热替换。

## 文件与回滚

- 安装目录：`/home/omg/.local/share/unifier-code/releases/0.0.6-32223567047a`
- 发布包：`/home/omg/.local/share/unifier-code/packages/0.0.6/unifier-0.0.6.tar.gz`
- SHA-256：`32223567047a1c0506b8b79fb732a135287605be384e9e9548aedb5a1ac803e9`
- 精确发布回执：`/home/omg/.local/share/unifier-code/packages/0.0.6/release.json`
- 回滚（先核验入口、菜单及新旧代码均未被后续修改）：

```bash
python3 /home/omg/.local/state/unifier/rollback/wps-v006-20261112T112316+0800/restore.py --apply
```

不加`--apply`仅检查回滚条件。回退恢复v0.0.5入口与原Dolphin helper路径，保留业务数据；不停止应用。已验证回滚条件，没有在真实日常入口上反复升降级。

## 保全与独立状态变化

11个CID保留，未启动容器、执行包事务或输入桌面操作；65个业务元数据/包装文件摘要保持，索引、修复工作树哈希、boot/KWin身份保持。部署写入仅新版本/包/回滚材料、全局入口链接和既有Dolphin菜单中的同字节helper版本路径。16个登记测试进程身份、10个测试单元均退出。

**保全例外已单独记录**：钉钉所在meeting实例在11:18:09从running变为exited/143；原worker消失，租约变为recovery-required，错误为 `application cancellation is unverified`。本轮第一条准备命令约11:18:50执行，入口切换约11:23，因此状态变化早于补丁准备执行和切换。停止来源UNKNOWN，未归因于用户或其他程序。没有擅自清理租约、启动实例或重启应用。其余10个容器状态保持。见[时间线](concurrent-state-change.json)和[审计](final-audit.json)，不能声称本轮所有日常状态均未变化。

字体/版式、云账号、其他目标和一期剩余验收不随发布版本号关闭。
