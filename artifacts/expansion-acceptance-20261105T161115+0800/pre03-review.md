# PRE03 固定Debian最小准入审核

当前源码产品部分与已通过802项的v0.0.3清单一致；新增测试装置通过52项策略/身份/局部清理与相关租约回归。监控者SIGKILL后ExecStopPost已在真实systemd、分离的宿主微型单元中验证清理，见monitor-loss/result.json；不是客体生命周期通过。
原固定Debian CID/UUID/generation、私有IPC/PID、唯一合成共享挂载、768MiB/192任务/1核、无自动重启、空lease/bridge、无中止锁已只读核对。既有unifier-tests.slice仍1GiB/128MiB swap/256任务/1核且空闲，不修改其限额。
监督者位于独立64MiB/16任务单元，250ms宿主和汇总cgroup采样，systemd独立总期限及ExecStopPost兜底；控制器192MiB/64任务、原客体及conmon/pasta/事件reader均须处于既有汇总slice。观察reader放在控制器预算，避免让Go工具挤占监督者预算。
先只做一次正式start、无副作用微型命令、正式stop。init/conmon的宿主PID/start/boot/cgroup和原生scope在首个应用动作前登记与核验。覆盖不符立即拒绝；已登记精确执行负责收尾，未建立身份则CLEANUP_UNKNOWN不扩大清理。不能据此放行Arch、GUI、包故障或长压。
本次属于完成保护装置后的受限准入验证，不使用旧线程保护器、不新建环境、不迁移项目根、不删中止锁。实际通过后才登记固定Debian CLI范围的PRE03结果并推进共享用例。
