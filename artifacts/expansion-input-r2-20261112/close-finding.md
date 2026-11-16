# SH10-CLOSE：文档窗口关闭后执行树未结束

状态：已观察，根因未隔离；阻塞SH10完整工作流。证据范围是本次工作树、固定Debian内的WPS、私有Xvfb :1115。

触发：正式APP入口启动Writer与合成DOCX，私有粘贴短标记，Ctrl+S后通过文件语义校验，向Writer窗口发送WM_DELETE_WINDOW。

预期：Writer执行正常退出，CLI持续运行，随后仍在同一CID中重开文档；最终最后租约关闭时按自动启动所有权停止实例。

实际：窗口消失，25秒内APP启动器未退出；已核验的客体组仍有wpsoffice、CEF辅助及wpscloudsvr进程。驱动超时退出1。保护器没有触发资源中止，随后执行精确清理，不能把清理停止记作应用正常关闭。

证据：writer_cli/04-close-one.request.processing、result.json、failure.png、live-binding.json、receipt.json；workflow-analysis.json保存PID/starttime、cgroup、粘贴与文档校验。前轮的输入失真已在本轮第一轮短文本中消除。

只读审查：application_leases.py等待完整客体执行树结束证明，仍有后代存活时保留租约符合现有安全合同。现有观测不足以证明应修改通用租约逻辑；需核对WPS正式退出动作/后台驻留行为及与APP入口的集成，并在下一次退出前后记录可见窗口与精确执行状态。新的真实复试必须保留本次失败、重新登记和准入。
