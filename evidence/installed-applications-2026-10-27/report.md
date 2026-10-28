# 本机已安装应用与关系测试补充

日期：2026-10-27，Asia/Shanghai。状态：**只读盘点完成，关系工作流未执行**。

用户指出应先查看本机已安装对象，并测试同名、同类型应用的关系。本轮读取 RPM、XDG 桌面入口、Flatpak、当前默认关联、限定目录的安装文件及选定命令解析；没有启动/安装/卸载应用、启动容器、更改默认关联或读取登录/凭据配置。

## 可直接复用的已安装对象

| 组 | 本机确认 | 测试价值 |
| --- | --- | --- |
| WPS | RPM `12.1.2.28080.AK.preread.sw-1`；文字/表格/演示/PDF/总入口 | 宿主原生基线，组件与入口别名、Office 默认、同名跨环境 |
| 微信 | RPM `4.1.13.9-1` | 现成宿主基线；后续同名客体实例与协议/文件入口 |
| 编辑器 | VS Code `1.137.0`、ZCode `3.11.2`、Qoder CN `0.2.3`、MarkText `0.23.0`、KWrite `26.08.1` | 同类型不同产品、命令名称、项目上下文、监听和保存竞争 |
| 办公对照 | LibreOffice Writer/Calc/Impress/Draw `26.2.6.3` | 与 WPS 共享文档格式，但保存/导出能力分别验证 |
| PDF/浏览 | Okular `26.08.1`、WPS PDF、Chrome `155.0.8059.84` | 同格式多候选、view/edit/convert 的区分 |
| 媒体 | mpv `0.41.0`、Haruna `1.8.1`、Dragon `26.08.1` | 同类型原生程序的默认、并发和文件可见性 |
| 主入口/URI | Clash Verge `2.5.2`、CC Switch `3.20.4`、VS Code、Qoder CN | 同名协议入口与主入口、默认处理器的保留与撤销 |
| 用户级 Flatpak | Steam `1.0.0.87`、Moonlight `6.2.0`、waywallen `0.4.4`、Wine `11.0` | 不同安装来源、app ID/branch/installation、portal/文件访问 |
| 其他已安装记录 | ChatGPT `26.1013.40934`、Grok Bot `0.75.0` | 安装版本与下载包版本的区别；不在当前工作会话里开关/降级它们 |

这是安装和注册现状，不是上述每个功能已通过 GUI 验收。ZCode、Qoder CN、Grok Bot、MarkText 等包的 License 字段为 unknown，不能据此断言闭源。Clash Verge、CC Switch 等对照也不计入闭源软件兼容率。完整源数据见 [RPM 清单](rpm-installed.json)、[筛选包](selected-installed-packages.json)、[桌面记录](desktop-entries.json)和 [Flatpak 清单](flatpak-applications.json)。

盘点覆盖 2,530 个 RPM 包、276 个 Application 类型桌面记录、4 个 Flatpak 应用；RPM 包含大量库，桌面记录包含隐藏处理器、系统设置模块和同产品多个入口，**这些数量不是独立 GUI 产品数**。纯便携软件和未注册程序没有全盘遍历；没有登记证据时只写“本轮未确认”。

## 已观察到的具体关系

1. **Chrome 同名两个入口属于同一安装。** `google-chrome.desktop` 与 `com.google.Chrome.desktop` 均由 `google-chrome-stable` 包拥有，Exec 同为 `/usr/bin/google-chrome-stable %U`，后者 NoDisplay=true。应按产品归组，同时保留各入口 ID 与现有默认，不能算两套 Chrome 或为去重删除包文件。
2. **WPS 套件、组件和别名需分开。** `wps-office-prometheus.desktop` 与 `wps-office-wps.desktop` 均属于 `wps-office`、共用 `/usr/bin/wps`，但分别用 `%F` 和 `%U`；表格、演示、PDF 各有入口。多个入口不等于多个安装实例，多个组件也不能在路由时合并丢失。
3. **Okular 是同名多格式入口。** 多个 `okularApplication_*.desktop` 共用查看器却声明不同格式。显示名称去重不能删掉 PDF、TXT 等精确 handler。
4. **当前默认与候选集合不能混为一谈。** DOCX/XLSX/PPTX 默认都是 WPS 总入口，但本次 `Gio.AppInfo.get_all_for_type` 枚举没有包含这个默认 ID。这里只确认这一观察，尚未证明默认打开失败；不能静默改为候选首项。PDF 默认 Okular，TXT/Python 默认 KWrite，HTML/HTTP/HTTPS 默认 Chrome，MP4 默认 Haruna。见 [关联快照](mime-associations.json)。
5. **MIME 相同不等于能力相同。** Office 候选里还可出现导入工具和 Ark，PDF 可被浏览器、查看器或编辑器处理；`view/edit/convert` 应按实际能力验证，不能仅凭 MIME 列表同级替换。
6. **公共启动器不是产品身份。** FreeCAD 与腾讯会议的桌面命令都先运行 Python，Flatpak 应用都先运行 flatpak，仍是不同产品/实例。Clash Verge 主入口与用户 URI 入口则是同一程序的不同入口角色。只按 argv[0] 或显示名称聚类都不够。
7. **检查进程的 PATH 是一个独立上下文。** 此次 agent 环境里 `soffice` 命中内置文档运行时 wrapper，而 `/usr/bin/libreoffice` 属于宿主安装。不能把这个结果外推到 KOS 菜单或交互终端；后续原生对照固定受核对的宿主程序路径，客体则核对自己的解释器与 PID/namespace。
8. **千问的现有入口有运行层差异。** 宿主 `qwen` wrapper 含 Wine、Flatpak、`.exe` 路线；下载目录另有原生 DEB。前者不能算 DEB 已安装，也不把本轮扩成 Windows/Wine 验收。这里只记录这个身份分类反例。

本次未观察到相同 desktop ID 在多个 XDG 目录覆盖，不能把同名不同 ID 的 Chrome 说成“已发生同 ID 遮蔽”。微信当前桌面文件没有声明 MIME/URI，查询的 weixin/wechat 协议默认均为空；后续只按实际版本提供的能力验收，未提供的能力如实记录，不新增虚假处理器充通过。

这些是只读元数据关系，窗口归属、实际文件交付、单实例 IPC、保存冲突等仍需真实工作流。相似 UI、相同 basename 或同一 MIME 都不能代替实际进程/目标证明。

## 本地安装文件可以减少重复准备

限定目录发现 14 个安装文件，已读取 RPM/DEB 头并计算 SHA256，见 [安装文件清单](local-installers.json)：

- **WPS RPM 可优先复用。** 本地 `wps-office-12.1.2.28080.AK.preread.sw.Personal-1-765475.x86_64.rpm` 与安装记录的 name/epoch/version/release/arch 一致，不可变头摘要同为 `5db6aded58c0277c1155f0f7bf411456a95f42ccc953973823fb64d88c1c394f`，可先做宿主/Fedora 客体同构建对照。详见 [复用锁定记录](wps-reuse-reference.json)。这不表示每个已安装可变配置文件与包载荷逐字节相同。
- 钉钉 `com.alibabainc.dingtalk 8.3.1.261008001` 与千问 `com.qianwen.otohime 3.5.6.113` 的 DEB 可以作为后续包格式限制素材；本次未确认它们在宿主原生安装。文件名、维护者字段不单独证明下载来源真实性。
- 腾讯会议 DEB 与已经迁移的 Debian 日常应用对应，是既有部署素材，不重复计作本轮新安装成功。
- MarkText `0.23.0`、Grok Bot `0.75.0`、CC Switch `3.20.4` 也有匹配已安装记录的 RPM；Grok Bot `0.70.2` 是另一个版本。
- 下载目录的 ChatGPT RPM 是 `26.917.61114`、`26.1007.21434`，均不是当前已安装的 `26.1013.40934`。不能因为“本地有包”就倒灌旧版本。
- 三个带 `(1)/(2)/(3)` 的 ChatGPT RPM 内容相同；两个 Chatbox AppImage 内容相同，见 [重复内容分组](duplicate-installer-content.json)。只记录，不删除这些原件，也不把下载的 AppImage 直接当成已安装应用。

## 调整后的测试顺序

1. **R0：直接复用宿主已安装应用。** 先把 Chrome/WPS/Okular/URI 入口的身份和默认图谱固定，再用合成文档与隔离测试配置做 WPS↔LibreOffice、编辑器、PDF、媒体的实际打开/保存/选择。当前盘点没有启动它们，也没有动默认项。
2. **R1：同构建的宿主↔客体。** Fedora/WPS 使用本地 RPM 优先，避免版本差异混入，这是原 9 个组合中 Fedora/WPS 的优先与对照，不额外重复安装一套；然后继续 Debian/WPS、Arch/Zoom 三发行版链路。没找到同构建 DEB 时，明确记录版本差异，不把 RPM 转包称为原生 DEB。原 9 个安装组合继续保留，Beyond Compare/飞书等新下载顺延。
3. **R2：同名跨环境及不同后端。** 微信、VS Code 的客体原始包尚需锁定；实际验证目标窗口/PID、单实例 IPC、项目解释器、私有配置、URI 和文件入口。Flatpak 单列 portal/路径可见性，不能靠发行版名称推断文件系统或安装源。
4. **退役与回滚。** 卸载/重建只在可再生客体 fixture 做，保留宿主既有软件和资料。受管默认变更保留 before/after 与冲突，不覆盖测试期间用户的新选择；取消客体实例不能按进程名称杀掉宿主同名应用。临时环境复用三套，收尾回到两个日常容器。

18 组关系和具体 oracle 见 [relationship-plan.json](relationship-plan.json)，全部实际工作流均标 NOT_RUN；本轮不将“已经安装”转换成“已经通过兼容验收”。

## 当前产品边界

当前 `src/multilinux/handlers.py` 的 provider 合同只有 `environment-app`。原生 GIO 基线可直接观测，受管客体菜单可以与宿主入口共存；但通用宿主 RPM/Flatpak handler 不能凭本轮盘点就算已实现。需要统一路由时，先补适配，再做验收，不能用外部宿主 relay 充数。候选身份应区分来源、包/构建、组件、入口与操作；客体还要绑定 UUID/gen/CID、app instance 和执行指纹。此身份表是测试设计，不是本轮新增 API。

本轮仅新增盘点/测试计划资料及更新计划入口；产品源码、已安装应用、默认关联和两套日常环境未修改。保全检查见 `preservation-check.json`。
