"""Language preference and the shortcut CLI's Chinese/English messages.

The machine contract (command names, target IDs, JSON field names and native
package-manager diagnostics) is never translated.  This module only translates
the user-facing shortcut layer and wraps native diagnostics with one sentence
in the selected language.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from unifier.errors import UnifierError


SUPPORTED = ("zh-CN", "en")
PREFERENCE_FILE = "settings.json"
LOCALE_FILES = ("/etc/locale.conf", "/etc/default/locale")
LOCALE_KEYS = ("LC_ALL", "LC_MESSAGES", "LANG")

_CATALOGS = {
    "zh-CN": {
        "cli.tagline": "多发行版应用环境命令行",
        "cli.usage": "用法：unifier <命令> [选项]",
        "cli.commands": "常用命令：",
        "cli.help_hint": "运行“unifier help 命令”查看用法；底层诊断命令仍为 env/pkg/app/project/view 等。",
        "cli.help_command": "help",
        "cli.help_summary": "显示帮助（可指定命令）",
        "cli.language_command": "language",
        "cli.language_summary": "显示或切换界面语言",
        "help.title": "{command} 帮助",
        "help.unknown": "没有命令 {command} 的帮助；请运行 unifier help 查看命令列表。",
        "help.lowlevel": "底层命令的帮助由原参数解析器提供（保持英文和既有协议）。",
        "help.options": "选项：",
        "help.usage_prefix": "用法：",
        "help.examples": "示例：",
        "error.arguments": "参数错误：{detail}",
        "error.noninteractive": "当前不是交互终端，缺少选择；请显式传入 --target（可重复）或 --none。",
        "error.eof": "没有读到交互输入，已取消。",
        "error.none_target": "--none 不能与 --target 同时使用。",
        "language.current": "当前语言：{language}",
        "language.source": "来源：{source}",
        "language.preference": "显式偏好：{preference}",
        "language.auto": "auto（未设置显式偏好）",
        "language.changed": "界面语言已切换为 {language}。",
        "language.auto_changed": "已清除显式语言偏好；当前语言：{language}（来源：{source}）。",
        "language.auto_help": "auto 清除显式偏好，并按宿主 locale 重新判断。",
        "language.invalid": "语言必须是 zh-CN、zh、en 或 auto。",
        "source.explicit": "显式语言偏好",
        "source.LC_ALL": "环境变量 LC_ALL",
        "source.LC_MESSAGES": "环境变量 LC_MESSAGES",
        "source.LANG": "环境变量 LANG",
        "source.locale.conf": "/etc/locale.conf",
        "source.default/locale": "/etc/default/locale",
        "source.default": "默认（英文）",
        "init.summary": "选择要安装的发行版（可多选，也可一个都不选）：",
        "init.option_none": "  0) 全部不选（只安装 CLI）",
        "init.prompt": "输入编号，用逗号或空格分隔；直接回车使用当前选择：",
        "init.current": "当前已启用：{targets}",
        "init.none": "未选择任何发行版：只保留 CLI，容器数为 0。",
        "init.confirm": "将准备 {count} 个发行版容器：{targets}。继续？[Y/n] ",
        "init.plan": "准备发行版：{targets}",
        "init.reuse": "{target}: 已有镜像 {digest}，复用本地镜像。",
        "init.pull": "{target}: 本地没有锁定镜像，拉取 {reference}。",
        "init.image_ready": "{target}: 镜像就绪 {digest}。",
        "init.private_label": "为自建专用目录设置 SELinux 容器访问标签：{path}。",
        "init.env_reuse": "{target}: 已存在唯一环境 {environment}，复用。",
        "init.env_create": "{target}: 创建唯一环境 {environment}（network={network}, desktop={desktop}）。",
        "init.ready": "{target}: 已就绪（容器已停止）。",
        "init.failed": "{target}: 初始化失败（阶段：{phase}）：{detail}",
        "init.continued": "失败的发行版记录已保留；再次运行 unifier init 可续跑。",
        "init.done": "初始化完成：已启用 {enabled} 个发行版，实际保留 {retained} 个容器。",
        "init.already": "所选发行版已就绪；未重复拉取或创建。",
        "init.invalid_target": "未知 target {target}；可选值：{choices}。",
        "init.conflict": "{target}: 环境记录存在但容器身份不匹配；已保留现状，请检查，不会自动替换。",
        "init.record_failed": "{target}: 环境已创建但初始化记录写入失败：{detail}",
        "configure.title": "更新启用的发行版（取消勾选不会停止或删除已有容器）：",
        "configure.done": "已启用 {enabled} 个发行版；实际保留 {retained} 个容器，失败 {failed} 个。",
        "configure.disabled_retained": "{target}: 已停用，容器保留",
        "configure.enabled": "{target}: 已启用",
        "configure.not_selected": "{target}: 未启用",
        "configure.failed": "{target}: 失败",
        "configure.ready": "{target}: 配置状态 {status}；容器 {container}",
        "status.enabled": "已启用",
        "status.disabled": "已停用，容器保留",
        "status.not_installed": "未安装",
        "status.never": "未配置",
        "status.adopted": "已承接（旧部署）",
        "status.failed": "失败",
        "status.blocked": "被策略阻止（数据保留）",
        "status.ready": "已就绪",
        "status.pending": "未完成",
        "status.running": "运行中",
        "status.stopped": "已停止",
        "status.missing": "容器缺失",
        "status.environment_missing": "环境记录缺失",
        "status.foreign": "身份不匹配",
        "status.none": "—",
        "list.header": "发行版\t配置状态\t容器状态\t专用目录",
        "list.footer": "已启用 {enabled} 个发行版；实际保留 {retained} 个容器。",
        "list.failed": "失败 {count} 个；可用 unifier init 续跑。",
        "list.none": "尚未初始化任何发行版；运行 unifier init。",
        "dirs.target": "{target}\t{path}\t{state}",
        "dirs.files_unmounted": "  新版专用 files/ 未确认绑定，实际共享目录见下方。",
        "dirs.share": "  共享目录：宿主 {host} → 客体 {guest}（{status}）",
        "dirs.share.writable": "可读写",
        "dirs.share.read-only": "不可写",
        "dirs.share.unavailable": "宿主路径缺失或不可用",
        "dirs.share.not-bound": "实际容器没有匹配的绑定",
        "dirs.share.unverified": "无法核验容器绑定",
        "dirs.share.none": "  没有可列出的用户共享目录。",
        "dirs.share.selected": "  path --files：{path}",
        "dirs.rootfs": "  系统视图：{state}（rootfs 不是复制目录；ls 使用只读视图）",
        "dirs.view_unmounted": "未挂载",
        "dirs.view_mounted": "已挂载：{path}",
        "dirs.not_configured": "{target}: 该发行版尚未配置；请先运行 unifier init 或 configure。",
        "dirs.unknown": "未知 target {target}；可选值：{choices}。",
        "path.not_configured": "{target}: 该发行版尚未配置。",
        "path.no_share": "{target}: 没有符合条件且已核验的宿主共享目录。请用 unifier dirs 查看。",
        "path.ambiguous": "{target}: 有多个可用共享目录。请用 unifier dirs 查看并手动选择。",
        "ls.not_configured": "{target}: 该发行版尚未配置；不会自动创建。",
        "ls.running": "{target}: 通过运行中容器的执行命名空间列出 {path}",
        "ls.stopped": "{target}: 容器已停止；使用只读视图列出 {path}",
        "ls.reuse_view": "{target}: 使用已有只读视图 {view}（不会撤销它）。",
        "ls.open_view": "{target}: 打开短期只读视图 {view}，完成后仅释放本次视图。",
        "ls.view_failed": "{target}: 无法读取只读视图：{detail}。视图记录和恢复信息已保留；运行 unifier view status 查看，必要时 unifier view recover。",
        "ls.view_busy": "{target}: 只读视图被占用或已存在（{detail}）。未撤销他人视图；运行 unifier view status 查看。",
        "ls.runtime_failed": "{target}: 在容器内列出 {path} 失败：{detail}",
        "ls.relative": "客体路径必须是绝对路径：{path}",
        "install.choose_target": "请选择用于安装的发行版：",
        "install.no_target": "没有匹配且已启用的发行版。请先运行 unifier configure 启用对应发行版；不会自动创建容器。",
        "install.target_disabled": "{target}: 该发行版未启用；请先运行 unifier configure。不会在后台新增容器。",
        "install.target_mismatch": "本地安装包格式 {format} 不接受 target {target}；候选：{candidates}。",
        "install.not_ready": "{target}: 环境尚未就绪；请先运行 unifier init。不会自动创建实例。",
        "install.noninteractive_target": "非交互安装需要 --target；可用发行版：{targets}。",
        "install.local": "{target}: 使用原生事务安装本地包 {file}。",
        "install.failed": "{target}: 安装失败（原生诊断保留原文）：{detail}",
        "install.package_error": "无法识别安装包：{detail}",
        "install.local_missing": "本地安装包不是文件：{file}",
        "install.packages": "{target}: 使用原生事务安装软件包：{packages}。",
        "install.transaction_failed": "原生包事务失败（阶段：{phase}）：{detail}",
        "install.finding.missing-package": "客体仓库找不到包 {name}。",
        "install.finding.missing-dependency": "客体尚未安装依赖包 {name}。",
        "install.finding.dependency-conflict": "客体包管理器无法满足依赖 {name}。",
        "install.finding.missing-hexdump": "安装脚本找不到 {name} 命令。",
        "install.finding.missing-library": "客体缺少运行库 {name}。",
        "install.finding.providers": "仓库查到的提供包为 {names}。",
        "install.hint.start-lookup": "如果客体已停止，查询前先启动它\n  {command}",
        "install.hint.lookup-package": "在客体查询可用候选\n  {command}",
        "install.hint.install-dependency": "在客体安装缺失依赖\n  {command}",
        "install.hint.install-provider": "选定提供包后在客体安装\n  {command}",
        "install.hint.repair-library": "按运行库名称在客体修复\n  {command}",
        "install.hint.recover": "先恢复未完成的包事务\n  {command}",
        "install.hint.show-transaction": "查看包管理器的事务记录\n  {command}",
        "install.transaction_ok": "原生包事务已完成：{transaction}。包事务成功不等于应用入口可用。",
        "install.bundled_unverified": "发现软件包自带库候选；实际加载和应用工作流仍未验证。",
        "install.entry_required": "无法确定应用入口；请显式传入 --exec /绝对路径/程序（包文件可用 --package 指定已安装包名）。候选：{candidates}",
        "install.choose_entry": "请选择应用入口（编号）：",
        "install.entry_discovered": "{target}: 从已安装包文件发现入口 {executable}。",
        "install.entry_explicit": "{target}: 使用显式入口 {executable}。",
        "install.entry_missing": "{target}: 入口 {executable} 没能登记：{detail}",
        "install.app_added": "已登记应用 {app}（target {target}，环境 {environment}）。",
        "install.app_reused": "应用 {app} 已登记，复用原启动记录。",
        "install.gui_no_desktop": "{target}: 容器没有桌面会话；桌面应用不会在无桌面容器中静默运行。请用 unifier init --desktop 创建桌面环境。",
        "install.app_name": "应用标识：{app}",
        "install.menu_added": "已加入软件列表：{app}。没有改动文件的默认打开方式。",
        "install.menu_kept": "应用 {app} 已在软件列表中，保留原菜单项。",
        "install.menu_hidden": "应用 {app} 已登记。安装包把自己标成不显示，所以没有放进软件列表。",
        "install.menu_failed": "应用 {app} 已安装并登记，但没有写入软件列表：{detail}",
        "install.deps_unresolved": "自动补运行库没有收敛：{libraries}。包已安装，仍继续登记入口并写入软件列表；这不证明应用能启动。",
        "install.app_conflict": "应用 {app} 已存在于其他环境或使用了不同入口；请显式改用 --app 或先移除旧登记。",
        "install.target_conflict": "{target}: 存在多个环境记录（{others}）；已保留冲突，请先明确承接或清理，不会自动选择。",
        "offer.title": "Unifier 安装管理",
        "offer.file": "文件：{name}",
        "offer.format": "格式：{format}",
        "offer.target": "白名单目标：{targets}",
        "offer.choice_y": "  y  用 Unifier 安装到对应发行版环境",
        "offer.choice_n": "  n  交给宿主原来的打开方式",
        "offer.prompt": "选择？[y/n] ",
        "offer.bad_choice": "请输入 y 或 n。直接回车不会安装。",
        "offer.install_completed": "安装完成。",
        "offer.install_failed": "安装未完成（退出码 {code}）。",
        "offer.install_close": "按回车关闭安装窗口。",
        "offer.host_native": "这是宿主发行版自己的安装包，不进入 Unifier 准备安装。",
        "offer.outside": "这个安装包不在当前宿主的 Unifier 白名单里，不进入准备安装。",
        "offer.handoff": "交给宿主打开方式：{handler}",
        "offer.no_handler": "没有可交回的宿主打开方式。未安装。",
        "offer.noninteractive": "当前不是交互终端。请使用 --yes 或 --host。",
        "offer.registered": "已登记外来安装包的打开方式：{formats}。宿主自己的包格式没有登记。",
        "offer.removed": "已恢复宿主原来的安装包打开方式。",
        "offer.nothing": "这个宿主没有可拦截的外来白名单包格式。",
        "target.record_mismatch": "{target}: 环境记录与 target/身份不匹配（{detail}）；已拒绝操作。",
        "run.app_missing": "找不到应用 {app}。",
        "run.failed": "运行 {app} 失败：{detail}",
        "run.target_missing": "{app} 不属于 target {target}。",
        "run.ambiguous": "应用 {app} 在多个已启用发行版中出现；请用 --target 指定。",
        "run.no_target": "没有已启用的发行版；请先运行 unifier init 或 configure。",
        "start.not_ready": "{target}: 环境尚未就绪；请先运行 unifier init。",
        "start.disabled": "{target}: 已停用；不会快捷启动。可用 unifier configure 重新启用。",
        "start.started": "{target}: 已启动（容器 {environment}）。",
        "start.running": "{target}: 容器已在运行。",
        "start.failed": "{target}: 启动失败：{detail}",
        "stop.not_ready": "{target}: 环境尚未就绪。",
        "stop.stopped": "{target}: 已停止（容器 {environment}）。",
        "stop.busy": "{target}: 有任务占用，未强制停止：{detail}",
        "stop.already": "{target}: 容器已停止。",
        "stop.failed": "{target}: 停止失败：{detail}",
        "stop.unknown": "{target}: 占用状态无法确认（{detail}）；拒绝停止，未强制操作。",
        "stop.package_busy": "{target}: 本 CLI 的原生包事务正在进行；拒绝停止以免中断安装。",
        "adopt.title": "高级承接：只读取旧部署记录并登记引用，不修改旧状态/挂载/菜单。",
        "adopt.done": "已登记 {target} 承接旧环境 {environment}（root={root}, UUID={uuid}, CID={cid}）。",
        "adopt.conflict": "{target} 已有不同身份的环境；冲突已保留，未覆盖。",
        "adopt.files_unmounted": "旧容器挂载中没有新专用目录 {files}；不会谎报已挂载。",
        "adopt.invalid": "旧记录无效或 target 不匹配：{detail}",
        "adopt.stale": "{target}: 承接引用已失效（{detail}）；拒绝使用同名替换或旧身份，未启动/修改旧部署。",
        "state.corrupt": "初始化记录损坏：{path}: {detail}；原文件已保留，请人工检查。",
        "generic.ok": "完成。",
    },
    "en": {
        "cli.tagline": "Multi-distribution application environment CLI",
        "cli.usage": "Usage: unifier <command> [options]",
        "cli.commands": "Common commands:",
        "cli.help_hint": "Run 'unifier help COMMAND' for usage. Low-level diagnostics remain env/pkg/app/project/view.",
        "cli.help_command": "help",
        "cli.help_summary": "show help, optionally for one command",
        "cli.language_command": "language",
        "cli.language_summary": "show or change the interface language",
        "help.title": "{command} help",
        "help.unknown": "No help for command {command}; run unifier help for the command list.",
        "help.lowlevel": "Low-level command help is provided by the original parser (English, unchanged protocol).",
        "help.options": "Options:",
        "help.usage_prefix": "Usage: ",
        "help.examples": "Examples:",
        "error.arguments": "Invalid arguments: {detail}",
        "error.noninteractive": "Not an interactive terminal and no selection was given; pass --target (repeatable) or --none explicitly.",
        "error.eof": "No interactive input was available; cancelled.",
        "error.none_target": "--none cannot be combined with --target.",
        "language.current": "Current language: {language}",
        "language.source": "Source: {source}",
        "language.preference": "Explicit preference: {preference}",
        "language.auto": "auto (no explicit preference)",
        "language.changed": "Interface language changed to {language}.",
        "language.auto_changed": "Explicit language preference cleared; current language: {language} (source: {source}).",
        "language.auto_help": "auto clears the explicit preference and re-detects the host locale.",
        "language.invalid": "language must be zh-CN, zh, en or auto.",
        "source.explicit": "explicit language preference",
        "source.LC_ALL": "LC_ALL environment variable",
        "source.LC_MESSAGES": "LC_MESSAGES environment variable",
        "source.LANG": "LANG environment variable",
        "source.locale.conf": "/etc/locale.conf",
        "source.default/locale": "/etc/default/locale",
        "source.default": "default (English)",
        "init.summary": "Select the distributions to install (multiple, or none):",
        "init.option_none": "  0) None (install the CLI only)",
        "init.prompt": "Enter numbers separated by commas or spaces; Enter keeps the current selection:",
        "init.current": "Currently enabled: {targets}",
        "init.none": "No distribution selected: CLI only, zero containers.",
        "init.confirm": "Prepare {count} distribution container(s): {targets}. Continue? [Y/n] ",
        "init.plan": "Preparing distributions: {targets}",
        "init.reuse": "{target}: locked image {digest} is present; reusing it.",
        "init.pull": "{target}: locked image is absent; pulling {reference}.",
        "init.image_ready": "{target}: image ready {digest}.",
        "init.private_label": "Preparing SELinux container access labels for the managed private directory: {path}.",
        "init.env_reuse": "{target}: unique environment {environment} exists; reusing it.",
        "init.env_create": "{target}: creating unique environment {environment} (network={network}, desktop={desktop}).",
        "init.ready": "{target}: ready (container stopped).",
        "init.failed": "{target}: initialization failed (phase: {phase}): {detail}",
        "init.continued": "The failed distribution record was kept; run unifier init again to continue.",
        "init.done": "Initialization complete: {enabled} distribution(s) enabled, {retained} container(s) retained.",
        "init.already": "The selected distributions are already ready; nothing was pulled or created again.",
        "init.invalid_target": "Unknown target {target}; choices: {choices}.",
        "init.conflict": "{target}: an environment record exists but its container identity does not match; it was left untouched and will not be replaced automatically.",
        "init.record_failed": "{target}: environment created but the installation record could not be written: {detail}",
        "configure.title": "Update enabled distributions (unchecking never stops or deletes an existing container):",
        "configure.done": "{enabled} distribution(s) enabled; {retained} container(s) retained, {failed} failed.",
        "configure.disabled_retained": "{target}: disabled, container retained",
        "configure.enabled": "{target}: enabled",
        "configure.not_selected": "{target}: not enabled",
        "configure.failed": "{target}: failed",
        "configure.ready": "{target}: configuration {status}; container {container}",
        "status.enabled": "enabled",
        "status.disabled": "disabled, container retained",
        "status.not_installed": "not installed",
        "status.never": "not configured",
        "status.adopted": "adopted (legacy deployment)",
        "status.failed": "failed",
        "status.blocked": "blocked by policy (data retained)",
        "status.ready": "ready",
        "status.pending": "incomplete",
        "status.running": "running",
        "status.stopped": "stopped",
        "status.missing": "container missing",
        "status.environment_missing": "environment record missing",
        "status.foreign": "identity mismatch",
        "status.none": "—",
        "list.header": "Distribution\tConfiguration\tContainer\tPrivate directory",
        "list.footer": "{enabled} distribution(s) enabled; {retained} container(s) retained.",
        "list.failed": "{count} failed; run unifier init to continue.",
        "list.none": "No distribution has been initialized; run unifier init.",
        "dirs.target": "{target}\t{path}\t{state}",
        "dirs.files_unmounted": "  New private files/ directory: NOT mounted or unverified. See shared directories below.",
        "dirs.share": "  Shared directory: host {host} → guest {guest} ({status})",
        "dirs.share.writable": "read-write",
        "dirs.share.read-only": "not writable",
        "dirs.share.unavailable": "host path missing or unavailable",
        "dirs.share.not-bound": "no matching container bind",
        "dirs.share.unverified": "container bind unverified",
        "dirs.share.none": "  No user shared directories to list.",
        "dirs.share.selected": "  path --files: {path}",
        "dirs.rootfs": "  system view: {state} (rootfs is not a copied tree; ls uses a read-only view)",
        "dirs.view_unmounted": "not mounted",
        "dirs.view_mounted": "mounted: {path}",
        "dirs.not_configured": "{target}: this distribution is not configured; run unifier init or configure first.",
        "dirs.unknown": "Unknown target {target}; choices: {choices}.",
        "path.not_configured": "{target}: this distribution is not configured.",
        "path.no_share": "{target}: no verified host shared directory meets the requested conditions. See unifier dirs.",
        "path.ambiguous": "{target}: several shared directories are available. See unifier dirs and select one manually.",
        "ls.not_configured": "{target}: this distribution is not configured; nothing was created.",
        "ls.running": "{target}: listing {path} through the running container's execution namespace",
        "ls.stopped": "{target}: container is stopped; listing {path} through a read-only view",
        "ls.reuse_view": "{target}: using existing read-only view {view} (it will not be revoked).",
        "ls.open_view": "{target}: opening a short-lived read-only view {view}; only this view is released afterwards.",
        "ls.view_failed": "{target}: cannot read the read-only view: {detail}. The view record and recovery information were kept; run unifier view status, and unifier view recover if needed.",
        "ls.view_busy": "{target}: the read-only view is busy or already exists ({detail}). No other view was revoked; run unifier view status.",
        "ls.runtime_failed": "{target}: listing {path} in the container failed: {detail}",
        "ls.relative": "guest path must be absolute: {path}",
        "install.choose_target": "Select the distribution for this install:",
        "install.no_target": "No matching enabled distribution. Run unifier configure to enable one; no container will be created automatically.",
        "install.target_disabled": "{target}: this distribution is disabled; run unifier configure first. No container was created in the background.",
        "install.target_mismatch": "Local package format {format} is not accepted by target {target}; candidates: {candidates}.",
        "install.not_ready": "{target}: environment is not ready; run unifier init first. No instance will be created automatically.",
        "install.noninteractive_target": "A non-interactive install needs --target; available distributions: {targets}.",
        "install.local": "{target}: installing local package {file} through the native transaction.",
        "install.failed": "{target}: installation failed (native diagnostics kept verbatim): {detail}",
        "install.package_error": "cannot identify the package: {detail}",
        "install.local_missing": "local package is not a file: {file}",
        "install.packages": "{target}: installing package(s) through the native transaction: {packages}.",
        "install.transaction_failed": "Native package transaction failed (phase: {phase}): {detail}",
        "install.finding.missing-package": "The guest repository could not find {name}.",
        "install.finding.missing-dependency": "The guest is missing dependency package {name}.",
        "install.finding.dependency-conflict": "The guest solver could not satisfy dependency {name}.",
        "install.finding.missing-hexdump": "The package script could not find {name}.",
        "install.finding.missing-library": "The guest is missing runtime library {name}.",
        "install.finding.providers": "Repository providers found for this library: {names}.",
        "install.hint.start-lookup": "If the guest is stopped, start it before querying\n  {command}",
        "install.hint.lookup-package": "Look up candidates in the guest\n  {command}",
        "install.hint.install-dependency": "Install the dependency in the guest\n  {command}",
        "install.hint.install-provider": "Install the selected provider in the guest\n  {command}",
        "install.hint.repair-library": "Resolve the runtime library in the guest\n  {command}",
        "install.hint.recover": "First recover the incomplete package transaction\n  {command}",
        "install.hint.show-transaction": "View the saved package-manager transaction\n  {command}",
        "install.transaction_ok": "Native package transaction completed: {transaction}. A successful package transaction does not prove the application entry works.",
        "install.bundled_unverified": "Bundled library candidates found; runtime loading and the application workflow remain unverified.",
        "install.entry_required": "Cannot determine an application entry; pass --exec /absolute/program explicitly (for a package file, --package names the installed package). Candidates: {candidates}",
        "install.choose_entry": "Select an application entry (number):",
        "install.entry_discovered": "{target}: discovered entry {executable} from installed package files.",
        "install.entry_explicit": "{target}: using explicit entry {executable}.",
        "install.entry_missing": "{target}: entry {executable} could not be registered: {detail}",
        "install.app_added": "Registered application {app} (target {target}, environment {environment}).",
        "install.app_reused": "Application {app} is already registered; reusing the launch record.",
        "install.gui_no_desktop": "{target}: the container has no desktop session; a desktop application will not be run silently in a headless container. Create a desktop environment with unifier init --desktop.",
        "install.app_name": "Application id: {app}",
        "install.menu_added": "Added {app} to the application menu. File associations were not changed.",
        "install.menu_kept": "Application {app} is already in the application menu; the existing entry was kept.",
        "install.menu_hidden": "Application {app} is registered. The package marked it hidden, so it was not added to the application menu.",
        "install.menu_failed": "Application {app} is installed and registered, but its application-menu entry was not written: {detail}",
        "install.deps_unresolved": "Automatic runtime-library repair did not converge: {libraries}. The package is installed, so its entry is still registered and put in the application menu; that does not prove the application starts.",
        "install.app_conflict": "Application {app} already exists in another environment or with a different entry; pass a different --app or remove the old record first.",
        "install.target_conflict": "{target}: multiple environment records exist ({others}); the conflict was retained and no automatic choice was made.",
        "offer.title": "Unifier package",
        "offer.file": "File: {name}",
        "offer.format": "Format: {format}",
        "offer.target": "Whitelist target: {targets}",
        "offer.choice_y": "  y  install it with Unifier",
        "offer.choice_n": "  n  hand it to the host opener",
        "offer.prompt": "Choice? [y/n] ",
        "offer.bad_choice": "Enter y or n. Pressing enter alone does not install.",
        "offer.install_completed": "Installation complete.",
        "offer.install_failed": "Installation did not complete (exit code {code}).",
        "offer.install_close": "Press Enter to close the installer.",
        "offer.host_native": "This package matches the host distribution and does not enter Unifier preparation.",
        "offer.outside": "This package is outside the Unifier whitelist for this host and does not enter preparation.",
        "offer.handoff": "Handed to the host opener: {handler}",
        "offer.no_handler": "No host opener is available. Nothing was installed.",
        "offer.noninteractive": "This is not an interactive terminal. Pass --yes or --host.",
        "offer.registered": "Registered openers for foreign packages: {formats}. The host's own package format was not registered.",
        "offer.removed": "Restored the previous host package openers.",
        "offer.nothing": "This host has no foreign whitelist package format to intercept.",
        "target.record_mismatch": "{target}: the environment record does not match its target/identity ({detail}); the operation was refused.",
        "run.app_missing": "Application {app} was not found.",
        "run.failed": "Failed to run {app}: {detail}",
        "run.target_missing": "{app} does not belong to target {target}.",
        "run.ambiguous": "Application {app} exists in more than one enabled distribution; pass --target.",
        "run.no_target": "No distribution is enabled; run unifier init or configure first.",
        "start.not_ready": "{target}: environment is not ready; run unifier init first.",
        "start.disabled": "{target}: disabled; the shortcut will not start it. Run unifier configure to re-enable it.",
        "start.started": "{target}: started (container {environment}).",
        "start.running": "{target}: container is already running.",
        "start.failed": "{target}: start failed: {detail}",
        "stop.not_ready": "{target}: environment is not ready.",
        "stop.stopped": "{target}: stopped (container {environment}).",
        "stop.busy": "{target}: tasks are still using it; not forcing a stop: {detail}",
        "stop.already": "{target}: container is already stopped.",
        "stop.failed": "{target}: stop failed: {detail}",
        "stop.unknown": "{target}: occupancy cannot be confirmed ({detail}); refusing to stop and not forcing it.",
        "stop.package_busy": "{target}: a native package transaction from this CLI is in progress; refusing to stop it and interrupt the install.",
        "adopt.title": "Advanced adoption: read a legacy record and register a reference only; old state/mounts/menus are not modified.",
        "adopt.done": "Registered {target} as adopting legacy environment {environment} (root={root}, UUID={uuid}, CID={cid}).",
        "adopt.conflict": "{target} already has a different environment identity; the conflict was retained and not overwritten.",
        "adopt.files_unmounted": "The legacy container mounts do not contain the new private directory {files}; not claiming it is mounted.",
        "adopt.invalid": "Legacy record is invalid or has the wrong target: {detail}",
        "adopt.stale": "{target}: the adopted reference is stale ({detail}); a same-name replacement or old identity is refused. The legacy deployment was neither started nor modified.",
        "state.corrupt": "Installation record is corrupt: {path}: {detail}; the original file was kept for inspection.",
        "generic.ok": "Done.",
    },
}


def normalize_language(value: str) -> str:
    lowered = str(value).strip().lower().replace("_", "-")
    if lowered in {"zh", "zh-cn", "zh-hans", "zh-hans-cn"}:
        return "zh-CN"
    if lowered in {"en", "en-us", "en-gb", "c", "posix"}:
        return "en"
    if lowered.startswith("zh"):
        return "zh-CN"
    if lowered:
        return "en"
    return "en"


def preference_path(root: Path) -> Path:
    return Path(root) / PREFERENCE_FILE


def read_preference(root: Path) -> str | None:
    path = preference_path(root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("language")
    if value in SUPPORTED:
        return value
    return None


def _read_locale_file(path: str) -> str | None:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    for key in LOCALE_KEYS:
        value = values.get(key)
        if value:
            return value
    return None


def resolve(root: Path, environ: dict | None = None,
            locale_files: tuple[str, ...] | None = None) -> dict:
    """Return language plus the source that selected it.

    Explicit preference wins.  Then LC_ALL, LC_MESSAGES, LANG, the distribution
    locale files and finally English.  Target names never choose a language.
    """
    values = dict(os.environ if environ is None else environ)
    preference = read_preference(root)
    if preference is not None:
        return {"language": preference, "source": "explicit",
                "preference": preference, "locale_value": None}
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = values.get(key)
        if value:
            return {"language": normalize_language(value), "source": key,
                    "preference": None, "locale_value": value}
    for path in (LOCALE_FILES if locale_files is None else locale_files):
        value = _read_locale_file(path)
        if value:
            label = "locale.conf" if path.endswith("locale.conf") else "default/locale"
            return {"language": normalize_language(value), "source": label,
                    "preference": None, "locale_value": value}
    return {"language": "en", "source": "default",
            "preference": None, "locale_value": None}


def save_preference(root: Path, language: str | None) -> None:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = preference_path(root)
    payload = {}
    if path.is_file():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(current, dict):
                payload = current
        except (OSError, json.JSONDecodeError):
            payload = {}
    if language is None:
        payload.pop("language", None)
    else:
        payload["language"] = language
    payload["schema_version"] = 1
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".unifier-", dir=root)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def translate(language_code: str, key: str, **values) -> str:
    catalog = _CATALOGS.get(language_code) or _CATALOGS["en"]
    template = catalog.get(key) or _CATALOGS["en"].get(key) or key
    try:
        return template.format(**values)
    except (KeyError, IndexError):
        return template


def source_label(language_code: str, source: str) -> str:
    return translate(language_code, "source." + source, source=source)


def command_specs(language: str) -> list[tuple[str, str]]:
    """Ordered shortcut command list used by top-level help."""
    return [
        ("init", _TEXTS[language]["init"]),
        ("configure", _TEXTS[language]["configure"]),
        ("list", _TEXTS[language]["list"]),
        ("dirs", _TEXTS[language]["dirs"]),
        ("path", _TEXTS[language]["path"]),
        ("ls", _TEXTS[language]["ls"]),
        ("install", _TEXTS[language]["install"]),
        ("offer", _TEXTS[language]["offer"]),
        ("run", _TEXTS[language]["run"]),
        ("start", _TEXTS[language]["start"]),
        ("stop", _TEXTS[language]["stop"]),
        ("adopt", _TEXTS[language]["adopt"]),
        ("language", _CATALOGS[language]["cli.language_summary"]),
        ("help", _CATALOGS[language]["cli.help_summary"]),
    ]


_TEXTS = {
    "zh-CN": {
        "init": "首次选择发行版并准备每个发行版唯一的日常容器",
        "configure": "更新启用集合；取消勾选不停止、不删除",
        "list": "查看发行版配置与容器状态",
        "dirs": "查看各发行版专用目录和系统视图状态",
        "path": "只输出专用目录的真实绝对路径",
        "ls": "列出客体系统真实子目录（不启动容器）",
        "install": "在已启用发行版中按原生事务安装软件",
        "offer": "外来安装包的 y/n 页；宿主自己的包格式不进入",
        "run": "运行已登记的应用（复用按需租约）",
        "start": "启动某发行版唯一容器",
        "stop": "停止某发行版唯一容器（有占用不强停）",
        "adopt": "高级：只读承接旧部署身份引用",
    },
    "en": {
        "init": "choose distributions and prepare one daily container per distribution",
        "configure": "update the enabled set; unchecking never stops or deletes",
        "list": "show distribution configuration and container state",
        "dirs": "show private directories and system-view state",
        "path": "print only the real absolute private directory path",
        "ls": "list real guest subdirectories without starting the container",
        "install": "install software with the native transaction in an enabled distribution",
        "offer": "y/n page for a foreign package; the host's own package format stays out",
        "run": "run a registered application with the existing on-demand lease",
        "start": "start the unique container for one distribution",
        "stop": "stop it, refusing to force a stop while tasks are using it",
        "adopt": "advanced: read-only adoption of a legacy deployment identity",
    },
}


def full_help(language: str) -> str:
    lines = [
        _CATALOGS[language]["cli.tagline"],
        "",
        _CATALOGS[language]["cli.usage"],
        "",
        _CATALOGS[language]["cli.commands"],
    ]
    for name, summary in command_specs(language):
        lines.append(f"  {name:<10} {summary}")
    lines.extend(["", _CATALOGS[language]["cli.help_hint"]])
    return "\n".join(lines)
