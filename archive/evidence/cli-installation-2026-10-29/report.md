# 纯 CLI 实施、本机验收与安装（2026-10-29）

结论：**首次发行版选择、每发行版单容器、专用目录与系统子目录查看、按发行版安装/运行、help 和中英文语言切换已实现，并安装到本机用户命令入口。** 本轮范围是 Fedora 宿主上的 CLI 与明确的原生程序链路；不是跨宿主、任意 GUI 或所有文件系统能力的通过结论。

机器结论见 [acceptance-result.json](acceptance-result.json)，最终源码/测试/配置指纹见 [source-frozen.json](source-frozen.json)。本轮没有提交或推送 Git。

## 使用

```bash
unifier help
unifier help init
unifier language zh-CN
unifier language en
unifier language auto

unifier init
unifier init --target debian13 --target arch --desktop
unifier configure
unifier list
unifier dirs
unifier path arch --files
unifier ls arch /usr

unifier install ./软件.deb
unifier install --target arch freecad
unifier run --target arch freecad
```

当前 `~/.local/bin/unifier` 指向此工作区的 `bin/unifier`。另一台机器在源码目录执行 `python3 tools/install_unifier.py` 注册入口；该脚本不会覆盖未知已有文件。初始化需要既有 rootless Podman 等运行条件，桌面应用应在首次配置时显式选 `--desktop`。

`language` 的显式偏好优先；auto 按 LC_ALL、LC_MESSAGES、LANG、发行版 locale 文件、英文回退依次判断。中文 locale 使用中文，其余使用英文。安装目标指当前运行 CLI 的 Linux，选择 Debian/Fedora/Arch 客体不突然切换界面语言。当前管理进程环境的 LC_ALL 为 C 类 locale 时，auto 显示英文，这是优先级的正常结果。

常用快捷命令帮助、交互和外层错误有中文、英文版本。原诊断子命令的详细帮助仍保留英文；原生包管理器和系统输出不伪造翻译。

## 实现与独立修复

- 初始化/配置以 target 为唯一用户选择，使用安装锁和持久状态；只拉取/创建选择的发行版，已就绪项复用。取消勾选保留实际容器和数据，分别展示启用数量与保留数量。
- 独立用户安装根支持 `UNIFIER_HOME`；新用户命令、底层诊断和 detached worker 正确使用同一根，保留源码诊断入口的兼容行为。
- 本地包格式先识别，再由目标内原生工具读取真正包名，不根据文件名猜测。按原生事务安装，发现/选择明确程序入口；同名应用在不同 target 的内部身份分开，原有实例授权仍绑定 UUID/代际/CID。
- 系统目录读取区分运行态执行命名空间与停止态只读 view。逐组件处理相对链接、`..` 和已投影的绝对链接，自动释放自己创建的短期 view；不撤销他人 view，不访问宿主同名系统路径。
- 停止在 lease → package → lifecycle 锁序内核对；活动任务、原生包事务、损坏租约和无法读取的进程身份均拒绝强停。原生安装与普通应用分别使用管理身份和 keep-id 用户身份。
- 新发现并修复了长应用别名截断碰撞、损坏登记被当空清单、help 参数位置、JSON 进度混入 stdout、旧身份引用重验证、desktop 入口参数丢失、安装根与 worker 不一致等边界。
- 在 SELinux 启用的宿主，仅给本安装自有、无符号链接且属于当前用户的专用 files 区设置容器访问标签。普通项目继续 preserve-host，不改原项目标签，不关闭整个容器的 SELinux 标签隔离。

## 验收证据

| 范围 | 结果与证据 |
| --- | --- |
| 回归 | [最终日志](unittest-accepted.log)：523 项，519 通过、4 条件跳过；原 456 项基线保持，新增 67 项 |
| 中英文与安装入口 | [已安装命令检查](installed-cli.json)：help、两语言切换、auto、adopt、list/dirs 的真实进程调用通过；语言优先级与无 runtime 帮助由离线合同覆盖 |
| 真实终端交互 | 主控以 PTY 实测中文 init 选择 0，得到 0 容器；中文 configure 输入 1、3 并确认后，启用 Debian/Arch，Fedora 停用但实际容器保留 |
| 选择与重复初始化 | [初次日志](init-first.log)、[真实续跑](live-first.json)：只选择 Debian/Arch 只创建两套，未选 Fedora 没有创建；重复操作复用 CID，随后明确 configure 才补 Fedora |
| 并发初次配置 | [并发结果](concurrent-init.json)：两个独立 CLI 同时初次选择 Fedora，真实只产生一个容器，两次均返回有效 JSON |
| 停止/运行态目录 | [修复后 CLI 检查](live-repair-check.json)：停止 Debian 的 `/bin`、停止 Arch 的 `/usr`、运行 Fedora 的 `/usr` 通过，JSON 可解析，正常停止通过 |
| 链接边界 | [独立探针](path-review-fixed.json) 四项通过；10 项专用测试覆盖链接、组件顺序、根限制、FD 释放和资源错误不得伪装空目录 |
| 原生安装及跨发行版同名入口 | [DEB/RPM 元数据安装与运行](native-metadata-live.json)：省略 --package，由原生工具识别包名，两个发行版分别登记并运行同名入口；实际 UID 均为 1000 |
| Arch 原生元数据 | [结果](arch-native-metadata.json)：真实 pacman 读取本地 Arch 包，获得原生包名，读取后仍停止；本项没有安装软件或触发全量升级 |
| 专用目录写回 | [写回结果](private-label-write.json)：SELinux Enforcing、无桌面的 Debian，普通应用写入中文文件，字节正确且 UID/GID 为 1000:1000 |
| 原日常部署 | [只读承接检查](adoption-check.json)、[最终保全](preservation-final.json)：旧环境的 UUID/代际/CID 引用有效，174 个既有部署/文档文件未变，原容器状态及启停时间未变 |
| 收尾 | [清理](cleanup.json)：5 个本轮隔离容器均按精确 CID/归属正常删除，不使用强制删除或全局 prune；实际恢复为两个原日常容器 |

测试包由 [build_probe_packages.py](build_probe_packages.py) 生成，只用于隔离客体；没有在宿主安装它们。临时环境的 31 个状态/事务/应用文件在 test-state/ 保存后再清理。基线旧容器、主目录、配置和菜单不参与删除。

## 当前日常部署与保全

默认安装根为 `~/.local/share/unifier`，已引用承接：

- Arch → 原 FreeCAD 部署 `freecad-arch`。
- Debian 13 → 原腾讯会议部署 `meeting`，应用入口仍为 `wemeet`。
- Fedora 44 未在默认日常安装根启用；本轮 Fedora 实测使用独立安装根，已清理。

承接只写新管理根的引用与专用目录，不重建旧容器、不迁移包数据库、不改菜单和原共享挂载。因此旧容器尚未挂入新建的专用 files，`dirs` 如实显示 `files_mounted=false`；原项目文件和私有 HOME 保留原位置，不能把新空目录冒充旧数据。新增挂载需要另行处理冻结契约。

修改前恢复备份：`/home/omg/文档/Unifier-backups/20261029-190435-before-cli-installation/`。145 个源码/测试/配置/文档文件逐项核验，Git 全引用 bundle 验证通过。dsh 首轮结果另存于本机 `.lab/agents/cli-installation-20261029/dsh-before-controller/`。

## dsh 记录和失败保留

施工使用 Command Code 的 `deepseek/deepseek-v4.1-flash`，会话 `session-38aded25-38ff-4ff7-a14a-dca2d6998c9d`。旧凭据引用认证失败后，调用级启动器改用本机当前有效引用；没有把密钥写入仓库。两个实施轮次均由主控主动中断进入独立修复/验收，exit 130 不作为成功；最终结论来自已冻结代码、测试与实机结果。原模型日志和凭据加载脚本位于忽略的 `.lab`，不提交原始思考/调用日志。

失败原件未覆盖：`offline-first.json` 的 help 参数失败、`path-review-before.json` 的链接反例、Fedora 同名应用登记冲突、`live-final.json` 的旧专用目录 SELinux 写回失败，均有后续修复证据。

`real-path-fixed.json` 中“/bin 列出的子目录必须非空”是一次错误的测试判据：当时读取已成功，而 `/bin` 通常没有子目录。后续按解析到 `/usr/bin`、返回码及真实目录语义检查，不把非空当成功条件。原记录保留，不能据此声称软件内容缺失或文件读取失败。

## 明确边界

- `ls` 当前列子目录及目录链接，保留普通文件系统错误，不是完整 GNU ls 的文件显示、排序和所有选项替代。
- 新命令没有替代旧投影的通知、快照、多卷一致性、完整元数据、IDE 和性能开放项。
- 新入口对旧 FreeCAD/腾讯会议的真实 GUI、媒体和摄像头没有在本轮重新验收；旧菜单保全不能替代新 GUI 验收。
- 其他宿主未测试。本轮 Arch 完成初始化、目录与原生包名读取，未重复执行完整 pacman 软件安装/全量升级；其原生驱动与禁止部分升级规则继续保留。
