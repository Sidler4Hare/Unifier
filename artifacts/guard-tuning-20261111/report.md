# 保护阈值调整与钉钉定位

2026-11-11，用户明确授权按已提出幅度调整规范并继续定位。

## 已完成的规范和执行器调整

- 启动 memory PSI full avg10 < 2%、some avg10 < 5%，连续 15 秒；原为 0.5%、2%、30 秒。
- 运行 PSI full > 5% 或 some > 20% 持续 10 秒中止；原为 3%、10%、2 秒。
- 作业内存达到硬限额 95% 持续 2 秒中止；原为 90% 连续两次采样。
- 保留 1 GiB 宿主可用内存、所有硬预算、任务数 80% 队列/90% 中止、OOM/硬限额事件、身份核验、独立监督、失联和期限条件。
- 更新共享 GuardPolicy、T1 与固定实例两个准入入口，以及规范和运行器文档。历史回执不修改；没有给日常容器改配额或部署产品。
- 用户随后明确选择手动重启现有登录会话并打开上传框。规范增加人工只读观察的明确边界：仅观察器受预算保护，不把日常应用说成受控，不认定为 T3/PRE03 通过，也不放行后续自动 GUI/包事务。

## 验证

`contracts/`：36 项相关合同通过，原生退出 0，无保护中止。覆盖新压力/内存持续时间、回落重置、阈值边界和保持不变的硬事件、任务数、身份规则。

`repo/`：891 项全仓检查，1 项失败、5 项跳过，原生退出 1，无保护中止。失败为 `test_agent_bridge_recovery.BridgeRecovery.test_dead_completed_session_reconciles_without_replay_and_remains_deduplicated`：预期 recovered，实际 kept-live-session。该请求桥用例不在本轮阈值修复范围内；本轮未改它，不将失败掩盖为通过，也未证实它在改前是否已失败。

`cef-scan/`：受保护的只读 ELF 定位成功，原生退出 0，无保护中止；没有执行钉钉二进制。上述三批回执均记录临时目录及工作 cgroup 已清理。

修改前原字节在 `before/`，本轮增量单独保存在 `this-turn.patch`，避免把既有脏改动当作本轮产物；Git 索引未改变。

## 新定位证据

`cef-static/xref.json` 记录实际钉钉携带的 libcef.so SHA-256。字符串交叉引用与局部反汇编证明，实际二进制确实包含标准 Portal → KDialog → toolkit 选择流程。

`xref-2.asm`：选择器入口约 ELF VA `0x7cfe0a0`；缓存地址 `0xc66e540`。非零时直接复用，0 时才探测；3=Portal、2=KDialog、1=toolkit。Portal 可用分支直接写入 3。

`xref-3.asm`：Portal 可用标志 `0xc66e568`；完成标志 `0xc66e569`；初始化保护标志 `0xc66e570`。

`live-globals.json`：对已核验 PID/start 的旧钉钉进程 526714 做只读 pread（未附加调试器、发信号或写内存），读取上述标量得到 choice=0、portal_available=1、portal_complete=1。此时当前根窗口列表没有找到钉钉上传框，所以尚不能以这一份快照证明本轮上传路径绕过标准选择流程。它至少排除了该时刻该标准选择器已经缓存 toolkit 的说法。

用户人工冷启动观察的后续证据另存于 `../dingtalk-manual-observation-20261111/`。完整动态调用栈仍不在只读标量/总线观察的覆盖范围内。
