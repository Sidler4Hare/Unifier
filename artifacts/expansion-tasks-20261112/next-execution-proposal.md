# SH10 输入修正后的续跑方案（未执行）

2026-11-12。上一份获批方案的单次 GUI 尝试已经结束：没有资源中止，但连续键盘输入保存成了错误标记，完整工作流未通过。归因尚未证明，不能直接判为产品损坏或仅仅认定自动化有错。

准备的修正代码：同目录 `next-writer-shared.py` 和 `next-private-paste.py`，SHA256见 `next-input-preparation.json`。使用私有 Xvfb :1114 的 CLIPBOARD 一次传输短标记，选择所有者与 xdotool 子进程均在原客体 cgroup，12秒期限、最多32个选择请求、文本最多256字节；从不连接宿主剪贴板或显示。锁定准确的 Writer 窗口标题，排除 WPS Docs 窗口。保存后仍解析 DOCX 核验标记和原文，失败立即结束并在保护允许时截图；不重写文档XML凑结果。

仅完成静态审查和语法检查，尚未执行新的输入实现；实际粘贴、两轮保存/正常关闭/重开与 CLI 存活只能由下一次真实工作流验收。

## 请求执行范围

- 原固定 Debian CID `e58754f0645ae1ff2924cb09a7264cc821e44893e7589a7fd0ebca09ce5f5e45`，原管理根 `.lab/guarded-expansion-20261103/environment`，UUID `ae147c3e-3e65-41ac-ac8d-9257c9202696`、generation 1。新增容器0。
- 沿用已经验证的任务预算：汇总384、客体256、控制器128。汇总1 GiB RAM/128 MiB swap/1CPU，客体768 MiB RAM/128 MiB swap、memory.high512 MiB，独立监督64 MiB，宿主保留1 GiB。PSI、80%关队列、90%中止与OOM规则不变。
- 新单元建议 `unifier-expanded-1112-writer-input-r1`、独有回执目录，600秒一次，最多两轮；不重新利用本轮已完成的spec、go或receipt。授权后将保护器的显式任务档位精确绑定该单元和原CID，并先通过离线合同。
- 已核实停止且零租约时，原生 `podman update --pids-limit 256`；用只读保存配置核验，启动后核验真实cgroup。结束后正式恢复租约并写回客体192/汇总256，保留所有证据。
- 再遇输入不一致、应用失败或保护中止，都结束这一尝试；不增额、不安装包、不操作日常桌面、不部署。

需要再次确认的原因是上一份具体方案写明“仅执行一次有依据的新尝试”，该次已执行完成；并非再次申请增加预算。旧授权与本轮执行事实见 `execution.md`、`writer_cli/receipt.json` 和 `final-audit.json`。
