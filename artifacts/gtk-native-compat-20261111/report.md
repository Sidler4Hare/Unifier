# Unifier GTK Native 兼容能力实现与验收

2026-11-11。已在既有 `/home/omg/文档/unifier-auto-host-share`、`codex/auto-host-share` 工作树实现；不是在当前主工作树覆盖另一组架构修复。已安装日常入口仍为 r4，没有部署或切换钉钉配置。

## 实现

新增应用级 `gtk-native-portal` 模式，复用已有 `unifier app file-chooser <id> <mode>` 命令及启动指纹机制。`native_library_launcher.py` 是独立发布到既有 Portal 桥的客体辅助程序：

- 仅允许已登记的 gtk3-native 规则；目标为 libgtk-3.so.0，别名为 libgtk-3.so。没有钉钉名称/版本/内存地址判断，也没有任意库名猜测。
- 客体加载器解析目标，校验 amd64 ELF、SONAME、六个 Native 函数及其实际库归属。宿主不执行或加载客体库。
- 在客体当前用户私有缓存创建版本化、可复用的库链接，仅将它加入本次应用 LD_LIBRARY_PATH。已有变量和 argv 保持；通过 execvpe 替换辅助进程，保留应用监督链。无系统库修改、开发包安装或二进制补丁。
- GTK_USE_PORTAL=1、已有 Portal 桥、实际同路径数据共享三者仍是前提；不同路径不假装可用。不会自动扩大共享目录，也不重写原生 Portal 返回路径。
- 普通 exec 与受监督启动均接入；包装准备失败标记 execution_started=false。设回 native 后不再包装或注入环境，既有运行进程不受影响。私有缓存保留避免打断其他应用，停用后无效。

钉钉 Elevator.sh 只设置 LD_PRELOAD，没有清除 LD_LIBRARY_PATH，静态上可保留这条映射；这不代替实际启动验收。

本轮七个文件及 SHA256 在 `files.json`，与改前对比在 `this-turn.patch`，既有文件恢复副本在 `before/`。使用说明位于目标工作树 `docs/GTK原生选择器兼容.md`。

## 验证

- `contracts/`：37 项定向测试全通过，退出 0。
- 合成库真实加载验证：只有 libgtk-3.so.0 的 fixture，经新启动器后 dlopen 与 GModule 均能使用 libgtk-3.so；验证 exec 后 PID、中文/空格/shell 元字符参数保持，缺符号及错误 SONAME 时拒绝执行。只编译测试库，不初始化真实 GTK 或 GUI。
- 元数据/缓存边界：截断或错误架构 ELF、坏字符串表、未知规则、冲突别名、不安全权限/目录链接被拒绝；旧目录不被覆盖，环境输入不变。
- 模式/启动边界：同路径和桥校验、默认 native 不变、停用回退、两种启动通道、执行指纹传递和准备失败清理。
- `repository/`：工作树仓库检查通过；238 个 Python 文件语法、42 个旧模块别名、5 个 CLI 入口通过；900 项测试中 5 项跳过、无失败，退出 0。适用于 auto-host-share 工作树，不代表当前主工作树此前的独立请求桥恢复失败已修复。
- 所有被验收文件的哈希在检查后仍一致。没有继续追加无目标回归。

## 本地 Qwen 与收尾

按用户本地模型偏好尝试一次仅写新测试文件的委派。有效路由经过脱敏 dump 核验为 local-server / swift-1.5-iq3_s / max，未回退远端付费模型。它在作业期限内未产出文件；最终单元 Result=timeout。保护回执另记录读取已退出 PID 的 /proc/stat 错误，command_result 缺失，因此本轮模型委派记为未完成/无有效验收，不当成模型正常退出或产品失败。未重试，由主控补齐测试。

三个作业均确认 cgroup_removed=true、temporary_removed=true；两个产品验收批次 reason=null。详细回执在 verification-summary.json。没有新增/启动/停止真实容器，没有自动启动/停止钉钉、操作桌面或发送文件，没有修改实际应用库或宿主会话。没有暂存、提交或推送；HEAD 仍 c947077f，暂存差异仍为空，既有脏改动保留。

## 真实工作流边界

本轮完成代码及离线验收。尚未部署到日常 r4，也未把实际钉钉设置为新模式。OpenFile/SaveFile、用户取消、所选共享文件能否在客体读取、GTK Native 是否仍因其他条件回退，需要在部署后通过明确的人工冷启动观察验证。本轮没有把合成库测试称为钉钉上传成功。
