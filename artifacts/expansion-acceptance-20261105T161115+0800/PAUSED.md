# 已按用户要求暂停

用户准备关机；此轮停止，不自动续跑。

- 所有18个本轮登记单元MainPID均为0且无活动ControlGroup；既有测试slice TasksCurrent=0。固定Debian CID最终收尾回执为exited/Running=false/PID=0，见live-minimum-retry/rescue.json。没有停止其他用户程序或操作宿主桌面/关机。
- 首批52项离线策略、局部清理及租约相关测试通过；修正conmon读取后20项相关回归通过（两批重叠，不相加）。真实systemd监督者SIGKILL后的独立收尾微型单元验证通过，仅为T1宿主机制证据。
- PRE03仍为PARTIAL，未放行共享CLI/依赖/GUI。第一次最小准入因ConmonPid字段假定错误而失败，已改为读取真实ConmonPidFile并在早期登记init/scope清理身份。唯一一次有依据重放又在实际guest cgroup限额核验处拒绝。两次均正常停止原CID，未进入微型应用执行或包事务。
- 最新live-binding记录表明init在libpod-完整CID.scope/container，systemd scope显示MemoryMax=805306368、MemorySwapMax=939524096、TasksMax=192，外层slice原设置swap上限134217728。下一步先核对scope、container叶及父slice的有效限额；当前检查可能把某一层原始swap值误当成有效上限。尚未证实是哪一字段导致差异，不改配额或放宽门槛凑通过。旧失败全部保留，本阶段已停止。
- Arch仍缺合规CPU/RAM测试配额；固定池不在同一管理根。媒体、账号、设备及其他扩测仍是原有阻断，不算完成。
- 此轮仅新增tools/reuse_guard_policy.py、tools/reuse_test_guard.py及两份相关测试和证据；原v0.0.3源码改动、发布包及旧证据保留，未提交/部署/推送。共享CLI驱动生成脚本发生语法错误，未执行、未创建共享输入或新应用登记。
- 原登记/租约/事务改前私密备份已从易失/tmp搬存到仓库.lab/agents/expansion-acceptance-20261105T161115+0800/private-backup（0700）；未复制模型密钥覆盖文件。所有必要源码/报告均在持久工作区。

重启后必须重新只读核对boot、CID/UUID/generation、租约、实际cgroup及资源准入；旧PID、systemd运行期属性和/tmp不能当作新现场。先修正并离线验证有效限额判定，再按规范登记后续尝试，不从半截命令直接继续。
