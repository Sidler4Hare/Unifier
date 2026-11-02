# 2026-10-31 本机扩展测试与卡点修复

本轮已接续今天的本机回执，修复阻塞并完成补测；**整套扩展计划仍为 PARTIAL**。宿主为 Fedora 44 KDE/Wayland，正向客体仅 Debian 13、Arch。Fedora 客体在本机只验证同派系拒绝，远端服务器没有参与本轮。

用户已解除本轮下载限制，授权见 [authorization.json](authorization.json)。历史修订 3 的 NOT_RUN 矩阵、旧失败回执和 v0.0.1 便携包均保留。当前执行汇总见 [execution-plan.json](execution-plan.json)。现有回执与本轮新修复分别记载，不把整个脏工作树归因于 Grok 或 DeepSeek。

## 接续时的进度与本轮结果

| 范围 | 已验证结果 | 证据与边界 |
| --- | --- | --- |
| 原有两个产品阻塞 | 修复后真实双客体 canary 通过 | 停止后 `ls` 的挂载身份验证、删除环境后错误显示 ready；见 [repair-handoff.md](repair-handoff.md)、`canary-attempt2/` |
| 共享项目文件 | 54/54 确定性回放通过 | 宿主、Debian、Arch 六个有向关系 × 直接写/原位写/原子替换 × 三轮；普通用户、字节/属主/inode 检查；[replays.json](shared-interwrite-attempt1/replays.json) |
| 开源 CLI | 12 个目标应用单元的核心安装及工作流通过 | 每客体 jq、ripgrep、fd、bat、SQLite、htop；非交互结果、错误退出、数据库事务及两次停止/重开见 `oss-attempt1/`；不是全部 CLI01–14 或 PKG01–14 |
| 交互终端补测 | 4/4 通过 | Debian/Arch 的 htop、bat，实际 PTY，就绪后改变尺寸、重绘、输入 q、退出 0；[result.json](pty-attempt3/result.json) |
| 本地原生包补测 | 2/2 通过 | 两个客体的真实 jq 原生包，文件名含中文、空格和字面量 `$;`，经产品安装/运行，求和结果 5；[steps.json](native-package-attempt3/steps.json) |
| 包边界 | 两个预期拒绝 | DEB→Arch、缺少 Alpine 适配器均拒绝，单列，不加入正向可用性计数 |
| 闭源 GUI | 5 个安装单元完成，完整工作流验收 0 | WPS、Beyond Compare、Debian/Arch Zoom 的先前窗口观察保留，本次未独立重放其保存工作流；飞书新增独立窗口验证，详见下节 |
| 最终离线回归 | 597 项运行，592 通过、5 跳过 | [final-check-repo.log](final-check-repo.log)，包含语法、旧名转发及入口检查；不是 GUI、性能或跨宿主验收 |

共享数据结果适用于显式共享 bind。两个客体各自注册私有项目根，再访问共同挂载的项目目录；没有把同一个根重复登记为两个项目，也没有将宿主发起的 `project run` 算作客体原生 Agent 或客体控制桥。

## 修复与独立复核

### 飞书启动 exit 133

Debian `bytedance-feishu-stable 7.72.23-0` 的 `libframe.so` 因缺少 `libasound.so.2` 加载失败，程序在 `lark_ex_main_aura.cc(347)` 触发 SIGTRAP。strace、gdb 和依赖记录见 [飞书专项报告](resume-20261031T153259+0800/feishu/report.md)。

在测试客体补装 `libasound2t64 1.2.14-1+deb13u1` 及依赖后，通过新登记的 `feishu-fixed` 应用，以普通用户 UID/GID 1000、原始可执行文件参数启动。成功路径没有 `--no-sandbox`。原 `feishu` 记录的哈希保留。

主控独立核验同一 CID、guest/host PID、WM_CLASS 及映射状态，截取该 X11 客户端窗口，确认出现 1648×940 飞书二维码登录界面。截图私存于 `.lab/agents/extension-local-20261031T111237+0800/`，不进 Git。详见 [primary-window-verification.json](resume-20261031T153259+0800/feishu/primary-window-verification.json) 和 [result.json](resume-20261031T153259+0800/feishu/result.json)。

状态为 **WINDOW_VERIFIED_ACCOUNT_BLOCKED**。账号、附件、协作、会议及音视频未验收。WM_DELETE_WINDOW 后窗口消失但后台继续；按精确 CID/PID/start/exe 核验后定向 TERM，记录为 FORCED_TEST_CLEANUP，不能算正常 Quit 通过。KWin 观察工具的回调问题单列为观察能力缺口，临时工具修改已撤回。这些窗口是 X11/XWayland 观察，不能据 Wayland socket 挂载宣称原生 Wayland 通过。

### 脱离终端的会话丢失窗口缩放

`project_sessions.py`、`runtime.py` 新增完整 SIGWINCH 转发：启动器→worker→所拥有的 Podman 代理；worker 将 resize 与取消分开，退出时恢复原 signal handler。补充两项回归。

原生对照与产品 A/B：调整前，裸 Podman 的 30×100 能变为 38×120，产品保持 30×100；修复后两者均为 38×120。见 [resize-before/result.json](resize-before/result.json)、[resize-after/result.json](resize-after/result.json)。之后的四个真实应用 PTY 工作流全部通过。初次过早输入 q 的烟测未被接受，原始失败/不充分证据保留在 `pty-attempt1/`、`pty-attempt2/`。

### 本地包文件名误用包名限制

`packages.py` 将原生文件路径与软件包标识分别验证。可打印 Unicode、空格和 `$;` 是单个 argv 的字面量文件名；控制字符及存在 Podman 路径分隔歧义的冒号仍拒绝，软件包名称和原生格式检查保持严格。新增三项回归，并用两个发行版的真实 jq 文件复验。

Debian 原生包由受认证 apt 仓库下载；Arch 使用精确客体中的原生缓存包并核对 `.PKGINFO`。来源、版本、摘要见 [native-jq-files/manifest.json](native-jq-files/manifest.json)。早期失败保留在 `native-package-attempt1/`、`native-package-attempt2/`：包含包清单漂移、并行维护干扰和文件名拒绝。最终串行执行通过，未弱化隔离检查或手改清单摘要。期间出现的宿主 `wine-bat.desktop` 属于其他工作，保留原样，未作为本轮客体副作用处理。

### 停止视图与缺失环境

`views.py` 处理 Podman 首次启动后消耗 Mounts.Mode 的真实行为：从同一绑定 CID 的 CreateCommand 对精确源/目标找回 z/Z 创建证据，同时保留实际挂载源、目标、读写及未知选项检查；缺失、冲突或歧义仍拒绝。真实生命周期探针见 `native-podman-mount-lifecycle.json`。

`shortcuts.py`、`i18n.py` 根据真实环境记录及容器身份生成列表/路径状态；缺失环境不再显示 ready/files_mounted，不自动重建或承接。双客体正常用户写入、停止、停止态 FUSE 浏览、重开各两轮，以及删除后缺失状态均已复验。

## 下载、保存与当前运行状态

- 五个官方安装文件共 **1,547,173,580 字节，约 1.44 GiB**。锁定来源、字节数及 SHA256 见 [official-download-lock.json](official-download-lock.json)。该数字只计安装文件，不是包含依赖、索引、调试工具及重试的总流量。大文件留在私有 `.lab/agents/`。
- DeepSeek 使用 dsh Command Code / `deepseek/deepseek-v4.1-flash` 实施指定修复，主控另外执行原生对照、回放和验收。代理会话、输出及修复前备份留在私有控制目录；已完成，没有以模型的完成消息代替测试证据。
- 两个原有日常容器（FreeCAD、腾讯会议）的 CID、镜像、挂载和完整 State 未变；19 个日常状态文件未变；原有暂存条目完全相同。只读 Git 操作可能刷新 index stat cache，因此不声明原始 index 字节相同。
- 修订 3 指名的 26 个历史输入摘要未变；没有最初全量归档哈希基线，不外推成全部历史文件审计。见 [preservation-audit.json](preservation-audit.json)、[final-source-sha256.json](final-source-sha256.json)。
- 两个新增扩测客体已通过产品命令正常停止，保留安装状态供后续阶段使用；当前为两个日常容器加两个停止的测试容器。见 [wave-close/result.json](wave-close/result.json)。这不是整套计划的最终拆除。
- 未暂存、提交、推送或部署本轮修改，未构建新发布包。`artifacts/unifier-0.0.1.tar.gz` 保留为旧包，**不包含这些修复**。

## 剩余验收与恢复入口

仍待实际执行：WPS Writer/Sheets/Presentation 编辑、PDF/版式和两轮保存重开；Beyond Compare GUI 合并保存；Zoom/飞书账号、附件、媒体与设备/portal；菜单、MIME、拖放、输入法关系；其他包格式、更新/删除/中断；共享 bind 以外的文件系统/ACL/xattr；客体原生 Agent；30 分钟带负载测试。客体到宿主/其他客体的受控桥未实现，不能用宿主手动转发替代。

收尾时宿主约 1.2 GiB MemAvailable，memory PSI full avg10 约 17.5%，同时存在其他用户工作。因此当前数据只作为功能验证，不作为受控性能比较、长压或空闲容器长期驻留验收。完整扩展仍不应标为完成。

继续时先核验 [fixture.json](fixture.json) 中 CID 与当前记录一致。该文件是创建时快照，里面的 running/包清单字段不代表收尾状态。管理入口和项目执行根必须区分：

```bash
cd /home/omg/文档/多兼容linux机
# 源环境/项目根；命令会按产品契约启动对应测试客体
UNIFIER_HOME="$PWD/.lab/extension-local-20261031/environments" bin/unifier project run shared-debian13 -- id
UNIFIER_HOME="$PWD/.lab/extension-local-20261031/environments" bin/unifier project run shared-arch -- id
# adopt 后的快捷管理根，仅查看当前选择和路径
UNIFIER_HOME="$PWD/.lab/extension-local-20261031/management" bin/unifier list --json
```

共享数据在 `.lab/extension-local-20261031/shared-project`；独立备份与代理记录在 `.lab/agents/extension-local-20261031T111237+0800`。后续按确切容器身份续测或清理，避免触及两个日常环境。
