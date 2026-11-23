# openSUSE Leap 16.1 适配计划

v0.0.9 计划加入第四个客体，初步范围为 Debian 13、Fedora 44、Arch、openSUSE Leap 16.1，直到 v0.1.0 前维持这四个发行版。适配从 `0.0.7.dev0` 开发基线出发，当前阶段准备目标契约和测试。现有三目标包管理和用户路径收口后，再启用新目标的实机准入。

2026-11-15 排期补充：v0.0.7 已发布，接续的 v0.0.8 集中于本机/服务器 CPU 与 Intel、NVIDIA V100、AMD HD 7470 硬件专项，同时交付统一目标目录、设备/能力协议和可复用探针。v0.0.9 在核验后的 v0.0.8 基线上接入 Leap；本文件原候选来源与镜像记录继续保留历史语义。[v0.0.8 准备交接](v0.0.8-硬件专项与v0.0.9准备.md#7-为-v009-准备什么)

前版只准备公共接口、CPU 最低要求判定和候选拒绝契约；zypper 驱动、Leap 实库/依赖适配、镜像启用及新增实例仍属于 v0.0.9。新客体必须重新验证硬件用户空间与宿主驱动组合，不能继承旧客体的通过状态。

## 候选镜像和范围

首选 openSUSE 官方 OBS 的普通 Leap 16.1 Base Container，`linux/amd64`，候选 ID 为 `opensuse-leap161`。已从 registry 读取并校验 OCI 索引、amd64 manifest、配置摘要，配置标明 openSUSE Project、Leap 16.1 Base Container，构建版本 `16.1.3.79`，压缩层合计 42,770,728 字节。镜像层尚未获取，产品镜像锁待获准后按同一摘要核验。候选输入见 [独立契约 fixture](../fixtures/opensuse-leap161/target-candidate.toml)与 [OBS 镜像源码](https://build.opensuse.org/package/show/openSUSE:Leap:16.1:Images/opensuse-leap-image?rev=a242da914461a46c641a5808d1e92dbe)。

首版限定 glibc、FHS、rootless Podman、同 Leap 版本的原地包更新。镜像的 `/etc/os-release`、运行命令、CPU ISA 要求、`C.UTF-8`、RPM 宏与数据库后端均由实际镜像验证。SUSE 派系仍适用同派系禁止规则。Tumbleweed、SLES、SLED 和 immutable Leap 不进入本期客体范围。[Leap 16.1 发行说明](https://doc.opensuse.org/release-notes/x86_64/openSUSE/Leap/16.1/html/release-notes-leap-161/)

## 实现顺序

| 工作 | 基线现状和接入要求 |
|---|---|
| 目录与准入 | `catalog.py` 已有 zypper 词表，`compatibility.py` 已识别 SUSE 宿主，准备目录仍限定三项。先让离线契约能描述 Leap，保持未知或未实现目标在执行前拒绝。接入时核齐 `tools/lab.py`、`install.py`、`packages.py`、`shortcuts.py` 和同发行版映射中的三目标硬编码，由准备好的目标清单统一驱动。旧三种 ID、数据目录和记录仍按原契约读入。 |
| 原生包驱动 | 增加 `ZypperDriver`，用 zypper 执行仓库安装、同版本更新、删除和本地 RPM，RPM 原生查询用于版本、文件清单及审计。按固定镜像实测 `--non-interactive` 在全局选项的位置及现有 PTY 入口如何移除它。记录原生退出码、缺包、未知签名、维护脚本、需要重启/重启服务提示，按命令解释信息性退出码。信任只来自已核验的仓库密钥，版本更新使用同发布线的 update 语义。 |
| 只读 RPM 库 | 先验证 `rpm --eval` 的数据库路径、后端和锁，以及 libzypp 与 RPM 的并发关系。当前适配器按 dnf 分派，只支持标准路径、有原生锁且已 checkpoint 的 SQLite。格式和锁约定匹配后共用解码器，并用该镜像的 `rpm -qa` 对齐。布局、非空 WAL 或原生锁不满足时明确拒绝，避免阻塞时用旧缓存当作实时清单。 |
| 文件和安装路由 | 当前 RPM 格式识别可复用。Fedora 与 Leap 都可用时显式选择目标，非交互调用要求 `--target`。在 Fedora/RHEL/SUSE 宿主双击原生 RPM 保持宿主处理，显式选择的跨派系客体仍走自身原生包验证。共用 RPM 查询前，先把包名识别、文件列表和数据库审计中按 Fedora ID 硬编码的分支迁到驱动层。 |
| 依赖补齐 | 先验证 zypper 的提供者查询、架构筛选、已安装包和文件路径证据。缺少可靠证据时给出明确的未支持结果，保留普通安装与诊断。用受信仓库的缺库与错误架构夹具验收，保持原有 apt、dnf、pacman 行为。 |

候选阶段先写描述拒绝、驱动 argv、信息性退出码、签名、同派系拒绝、双 RPM 路由和旧记录兼容的离线用例。zypper 的全局选项与退出语义以固定镜像中的帮助及 [上游手册](https://github.com/openSUSE/zypper/blob/master/doc/zypper.8.txt) 双重核对。

## 启用和验收

| 顺序 | 验收目标 |
|---|---|
| SUSE01 镜像和初始运行 | 固定 amd64 digest，验证发布 ID、版本、仓库、基础工具、CPU、数据目录与 UID 映射。初次选择、私有安装根和原三目标升级读取合同通过。 |
| SUSE02 停止态与同管理根 | 同一原始管理根下至少一对已准入跨派系环境。Leap 实库与原生查询对齐，旁路写入、busy/日志、挂载遮蔽、运行态同伴分别有拒绝证据。同伴未被唤醒或改包。 |
| SUSE03 包事务与恢复 | 正式入口跑仓库安装、同版本更新、删除、已签名本地 RPM 1.0 → 2.0 → 删除。验证库、文件、版本、事务及维护输出，分别覆盖签名、网络、缺包、脚本失败和一次获准的受控中断恢复。 |
| SUSE04 生命周期与用户路径 | 固定实例、三 CLI 同容器、租约 3→0、最后退出自动停止。再验收选择、安装、应用入口、启动、退出。首次 GUI 按独立显示和资源预算单独准入。 |
| SUSE05 四目标回归 | 原三目标的 CLI、RPM 宿主分流、同派系拒绝、旧数据和候选包构建均通过；记录四目标支持边界、升级与回退证据。 |

首次实机前核对能否复用合规既有 Leap 实例。确无可复用实例时，单独提交最多一台、指定异派系宿主、固定 digest、挂载、资源、保留和退出方案，取得明确授权再建立。当前 fixture 位于 `fixtures/`，产品目标表、镜像锁和可创建范围维持 v0.0.7 基线。[当前状态](../docs/当前状态.md) · [测试规范](../docs/测试禁令与规范.md)
