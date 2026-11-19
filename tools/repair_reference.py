#!/usr/bin/env python3
"""Look up historical Unifier repairs without inspecting or starting environments."""

import argparse
from dataclasses import asdict, dataclass
import json


@dataclass(frozen=True)
class Repair:
    release: str
    title: str
    symptom: str
    source: tuple[str, ...]
    evidence: tuple[str, ...]
    applicability: str


REPAIRS = (
    Repair(
        "0.0.1", "受控文件操作的写租约",
        "Python 3.11/3.12 缺少 fcntl.F_SETOWN_EX，写租约初始化报错。",
        ("src/unifier/lease.py::_set_owner_tid", "src/unifier/lease.py::WriteLease"),
        ("docs/v0.0.1.md", "tests/test_controlled_move.py"),
        "确认错误来自受控移动的租约，现行代码使用同一个 Linux UAPI 操作。其他文件系统租约失败需看实际 errno。",
    ),
    Repair(
        "0.0.1", "停止视图目录、rename 和读取缓存",
        "受限链接导致可读兄弟项消失、外部 rename 后相对链接解析错误、同尺寸更新后内容仍旧。",
        ("src/unifier/projection.py::ProjectionObjects._snapshot",
         "src/unifier/projection.py::ProjectionObjects.readlink",
         "src/unifier/fuse_view.py::FuseServer._revalidate_attr"),
        ("artifacts/v0.0.1-20261030/report.md", "tests/test_release_regressions.py",
         "tests/test_projection_rename.py"),
        "先确认是停止态只读投影。普通共享目录走 bind；旧 FD 仍指向原对象，已有 mmap 单独读时没有自动重验证。",
    ),
    Repair(
        "0.0.2", "包事务中断与恢复屏障",
        "包管理超时或 SIGTERM/SIGHUP/SIGINT/SIGKILL 后清单 unknown、needs-recovery，下一次写包被拒绝。",
        ("src/unifier/service.py::EnvironmentService._native_package_journal",
         "src/unifier/service.py::EnvironmentService._recover_packages_locked",
         "src/unifier/cli.py::cmd_pkg_recover"),
        ("artifacts/expansion-fix-20261101/report.md", "tests/test_package_recovery.py"),
        "先用 env inspect、pkg log 和 pkg show 核对同一环境及失败事务。按实际数据库状态走 pkg recover；"
        "恢复要求原执行已结束及原生数据库通过审计。v0.0.7 补齐三发行版恢复细节。",
    ),
    Repair(
        "0.0.2", "原生安装提示与终端缩放",
        "交互确认丢失，apt-get 内层 PTY 保留旧尺寸，dpkg postinst 看不到终端缩放。",
        ("src/unifier/packages.py::package_command", "src/unifier/packages.py::package_environment",
         "src/unifier/native_io.py::run_native"),
        ("artifacts/expansion-fix-20261101/report.md",
         "tests/test_native_io.py::test_native_prompt_input_resize_and_terminal_restoration"),
        "核对正式包事务连接了真实终端。交互 apt-get 使用 Dpkg::Use-Pty=0；非交互和 JSON 模式沿用对应原生参数。",
    ),
    Repair(
        "0.0.3", "从日志定位动态加载缺库",
        "cannot open shared object file: No such file or directory，运行期 dlopen 的 SONAME 未进入静态 ELF 扫描。",
        ("src/unifier/dependency_diagnostics.py::parse_diagnostics",
         "src/unifier/dependency_diagnostics.py::read_diagnostics",
         "src/unifier/dependencies.py::DependencyRepair._run", "src/unifier/cli.py::cmd_pkg_repair"),
        ("docs/v0.0.3.md", "tests/test_dependency_diagnostic_repair.py"),
        "对当前客体和应用的日志先用 pkg repair-deps --from-log FILE --diagnose-only。ABI、权限及不明插件错误需另查；"
        "唯一提供者装好后仍需验证实际加载及工作流。",
    ),
    Repair(
        "0.0.3", "同管理根包事务协调与应用执行树归属",
        "并发包事务观察到错误的同伴变化，或主进程退出后后台子进程仍持有 APP lease。",
        ("src/unifier/operation_lock.py::management_lock", "src/unifier/runtime.py::PodmanRuntime.read_inventory_readonly",
         "src/unifier/application_supervisor.py::run", "src/unifier/application_leases.py::recover_applications"),
        ("artifacts/architecture-repair-20261105T111850+0800/report.md", "tests/test_package_coordination.py",
         "tests/test_application_lifecycle.py"),
        "分别核对包观察阶段和 APP/PID 归属。运行态同伴的一致清单仍受限；应用用 app leases 查活动租约。"
        "默认 wait 会等待其余后代，确需前台退出联动时见 v0.0.6。",
    ),
    Repair(
        "0.0.3", "受控 Agent 请求与中断恢复",
        "客体跨宿主执行缺少授权路径，或请求中断后执行归属未确认、请求账本无法自动收尾。",
        ("src/unifier/agent_bridge.py::RequestLedger", "src/unifier/agent_bridge.py::AgentBridge.recover_requests",
         "src/unifier/exec_control.py"),
        ("docs/v0.0.3.md", "tests/test_agent_bridge_recovery.py", "docs/当前状态.md"),
        "0.0.3 引入 host-info/project-run 桥，后续发布补显式请求恢复。调和时看绑定身份和执行退出证明；"
        "待确认的请求仍阻断，不会自动重放。",
    ),
    Repair(
        "0.0.4", "安装后软件列表登记",
        "安装成功且应用可运行，但 KDE 软件列表/应用栏没有菜单；NoDisplay、Hidden 或无效绝对图标路径。",
        ("src/unifier/shortcuts.py::_publish_installed_menu", "src/unifier/app_entries.py::host_menu_icon",
         "src/unifier/service.py::EnvironmentService.export_app", "src/unifier/desktop.py::render_desktop_entry"),
        ("docs/v0.0.4.md", "tests/test_cli_shortcuts.py::test_hidden_package_stays_out_of_the_application_menu"),
        "先用 app show 核对已登记的启动入口。应用主动隐藏时只有启动登记；已有可用入口需要显式菜单时用 app export --menu-only。"
        "遇到 ELF 扫描失败另查 0.0.7+wayland.3。",
    ),
    Repair(
        "0.0.5", "Portal 超时、桥接重建与消息预算",
        "选择器取消或超时无终态、迟到 Response 遗留快照、交付进程死后遗留旧租约、队列预算超限。",
        ("src/unifier/portal_file_bridge.py::Client.expire_request", "src/unifier/portal_file_bridge.py::Client.filtered",
         "src/unifier/on_demand.py::start_bridge", "src/unifier/portal_delivery.py"),
        ("docs/Portal首批修复验收-20261111.md", "tests/test_portal_file_bridge.py", "tests/test_portal_bridge_recovery.py",
         "docs/GTK原生选择器兼容.md"),
        "如果钉钉或 QQ 又出现应用内文件选择框，先核对菜单入口使用的版本，查询 env portal-bridge 和 app file-chooser。"
        "钉钉历史使用 gtk-native-portal。确认请求已走桥接后，再排查 300 秒超时和 32 MiB 消息预算。授权及撤销见 v0.0.7。",
    ),
    Repair(
        "0.0.5", "存储引用恢复和 FUSE worker 文件描述符",
        "停止态清单留下存储引用，或投影视图触及软 FD 限额 `Too many open files` / EMFILE。",
        ("src/unifier/runtime.py::PodmanRuntime.recover_inventory_storage", "src/unifier/projection.py::FdBudget",
         "src/unifier/fuse_view.py::run_fuse"),
        ("docs/v0.0.5.md", "tests/test_inventory_recovery.py", "tests/test_fuse_nofile_limit.py"),
        "显式 pkg recover-storage 只释放归属已证明的引用，模糊记录保留。FD 故障先定位实际投影 worker 和限额。",
    ),
    Repair(
        "0.0.6", "前台退出后回收 WPS 辅助进程",
        "WPS Writer 窗口已关但租约、辅助进程和容器不退出，或既有启动器提前返回导致错误套用 stop-children。",
        ("src/unifier/service.py::EnvironmentService._validate_app_exit_policy",
         "src/unifier/application_supervisor.py::run"),
        ("artifacts/wps-close-regression-20261112/report.md", "tests/test_application_exit_policy.py"),
        "仅给持续等待前台界面结束的按需命令登记 --exit-policy stop-children。默认 wait。"
        "回执覆盖固定 Debian/WPS 版本的编辑保存、重开及 TERM 收尾；实际升级 KILL 时成功主命令也返回 125。",
    ),
    Repair(
        "0.0.7", "三目标停止清单和原生事务恢复",
        "停止态 dpkg/pacman/RPM SQLite 清单不一致，RPM WAL 未 checkpoint、dpkg 编号日志待合入、Arch 后置动作未验证。",
        ("src/unifier/runtime.py::PodmanRuntime.read_inventory_readonly", "src/unifier/inventory_backends.py::backend_for",
         "src/unifier/service.py::EnvironmentService._recover_packages_locked"),
        ("docs/三目标包管理收口-20261112.md", "artifacts/pkg-recover-journals-20261114T135636+0800-dnD9ha9r/report.md"),
        "查实际客体、停止/运行态、原生锁和事务结果，再选对应诊断及 pkg recover。宿主 Python rpm 模块、"
        "标准 SQLite 库及 checkpoint 是 Fedora 停止态读取条件；Arch 原生维护收尾需要证据。",
    ),
    Repair(
        "0.0.7", "选择文件、只读交付和撤销",
        "Portal 选到宿主文件但客体不可读；取消授权后还能否重新打开，或编辑副本后没有写回原件。",
        ("src/unifier/file_access.py::decide_selection", "src/unifier/portal_delivery.py::SelectionStore.deliver",
         "src/unifier/file_access_cli.py::AccessControl.revoke"),
        ("docs/统一文件访问与权限系统-20261113.md",
         "artifacts/v007-candidate-20261114T142129+0800-JZE0NirY/portal-real-X3DO8HSw/report.md"),
        "用 file access list 核对 lease 和 grant。未共享文件可获只读副本；Debian Zenity 回执验证撤销后下一次打开为 ENOENT，"
        "已有 FD 及已读内容有独立生命周期。",
    ),
    Repair(
        "0.0.7+wayland.1", "首次启动的 Wayland socket 换名",
        "crun 因旧宿主 Wayland socket 的 bind 源缺失而退出，wayland-0 切到 wayland-1，dpkg-deb 尚未探测包名。",
        ("src/unifier/service.py::EnvironmentService.start", "src/unifier/wayland.py::plan_refresh",
         "src/unifier/wayland.py::start_with_refresh"),
        ("artifacts/wayland-hotfix-20261116-G0aZQjtF/report.md", "tests/test_wayland_refresh.py"),
        "核对已停止实例的登记、当前会话 socket 与实际 bind。通过 Unifier 启动时每次只刷新宿主源，"
        "继续使用已登记的客体路径和 CID；包名探测已成功但正式安装失败时继续查 .2。",
    ),
    Repair(
        "0.0.7+wayland.2", "两次启动仍引用旧 Wayland 源",
        "首次 dpkg-deb 已返回 linuxqq，停止后正式安装第二次启动在 runtime 阶段失败；Podman inspect 看起来已有新源。",
        ("src/unifier/wayland.py::is_managed_wayland_bind", "src/unifier/wayland.py::plan_refresh",
         "src/unifier/wayland.py::_rewrite_mount"),
        ("artifacts/wayland-hotfix-r2-20261116-A3Rnxpbk/report.md",
         "tests/test_wayland_refresh.py::test_package_probe_and_install_both_refresh_the_same_instance"),
        "停止后的 inspect 可能显示上次有效 OCI 挂载，而下次从原配置生成。证据应同时包含两次启动、"
        "已登记的客体目标及同一私有运行目录的宿主 socket。",
    ),
    Repair(
        "0.0.7+wayland.3", "ELF ipc.node 扫描及补登记菜单",
        "linuxqq 实际已经安装，ipc.node / 非 .so SONAME 导致依赖扫描先退出，KDE QQ 菜单缺失。",
        ("src/unifier/dependencies.py::parse_scan", "src/unifier/dependencies.py::validate_library",
         "src/unifier/shortcuts.py::_can_register_after_dependency_failure"),
        ("artifacts/qq-menu-repair-20261116-dTsmmw1N/report.md",
         "tests/test_dependencies.py::test_node_soname_is_scanned_and_only_so_names_are_queried"),
        "先核对包清单、本次诊断和恢复状态。ELF 名称读取允许 .node，原生仓库查找仍受库名规则限制。"
        "包已核验且无恢复屏障时可继续登记菜单，扫描报错仍返回。已装 QQ 的历史补登记没有重新安装包。",
    ),
    Repair(
        "0.0.7+wayland.4", "XWayland 上的宿主 Fcitx XIM",
        "QQ 中文输入法无反应、GTK_IM_MODULE 或 XMODIFIERS 未传入，X11 和 Wayland 同时存在。",
        ("src/unifier/input_method.py::apply_input_method", "src/unifier/service.py::EnvironmentService.set_app_input_method",
         "src/unifier/cli.py::cmd_app_input_method"),
        ("artifacts/host-input-method-final-20261116-8yqH4ZvH/report.md", "tests/test_input_method.py"),
        "用 app input-method ID 查偏好。已确定使用 XWayland 时可选 host-xim；auto 仅在明确用 X11 时选择 GTK XIM。"
        "QQ 已验证 XIM 握手；字体缺字、宿主 locale 或原生 Wayland 输入走各自的路径。",
    ),
    Repair(
        "0.0.8b1", "客体应用监督器缺 Python 3",
        "guest prerequisite failed: ERROR application-python3-unavailable，菜单已生成但 /opt/QQ/qq 尚未执行。",
        ("src/unifier/service.py::EnvironmentService._application_python_ready",
         "src/unifier/shortcuts.py::cmd_install", "src/unifier/runtime.py::PodmanRuntime.start_application_exec"),
        ("artifacts/v008-qq-guest-python-20261117-R3SDQBe8/report.md",
         "artifacts/v008b1-runtime-hotfix-20261117-zKD7Mqu1/report.md",
         "tests/test_cli_shortcuts.py::test_local_app_install_adds_and_checks_missing_guest_python"),
        "Beta 1 原包与运行时热修都显示 0.0.8b1，需核对实际构建。热修在快捷应用安装、按包名 repair-deps 中补并验证解释器。"
        "Debian/Fedora 用 python3，Arch 用 python 并走完整同步升级。新建最小客体的自动补齐待实机重放。",
    ),
    Repair(
        "0.0.8b1+render.1", "QQ GPU 超时与单 DRM render node",
        "QQ GPU 进程约每 15 秒退出，KMS CREATE_DUMB Permission denied；XWayland 传了 DRI3 FD，客体无对应节点且 drmGetDevice2 返回 ENODEV。",
        ("src/unifier/service.py::EnvironmentService.configure_render_node", "src/unifier/render_node.py::rewrite_spec",
         "src/unifier/render_node.py::start_with_render_node"),
        ("artifacts/v008beta-clean-install-20261117-MDi9pIbP/report.md",
         "artifacts/qq-standard-20261118-HlbtB4ve/report.md", "artifacts/qq-render-node-daily-20261118-Vn2eRc48/report.md"),
        "历史 Meeting 的登记和 Podman CreateCommand 均有原生 /dev/dri/renderD128；新日用 Debian 原先没有。"
        "对同特征、已停止的 rootless 桌面实例，核对节点身份、宿主权限和现有映射后才用 env render-node。"
        "补丁由 Unifier 启动时加单节点；已有原生映射或使用不同 GPU 时重新核对。历史实测配置保留 QQ 默认参数。",
    ),
    Repair(
        "NextKde", "KOS 托盘没有 DBusMenu 的点击转发",
        "QQ 托盘左右键都没反应。右键项暴露 ContextMenu，但没有 DBusMenu。"
        "左键曾出现连续 Activate(0, 0)。改为单次转发后，传屏幕中心仍无法重放点击。",
        ("NextKde/shell/desktop/modules/bar/SysTray.qml::openMenu",
         "NextKde/platform/src/daemon/PlatformServerTray.cpp::PlatformServer::handleTrayOperation",
         "NextKde/platform/src/daemon/PlatformServerTray.cpp::trayPointer",
         "NextKde/platform/src/daemon/PlatformServerTray.cpp::xwaylandTrayOutput",
         "src/unifier/on_demand.py::_start_bus_bridge"),
        ("NextKde/platform/tests/test_tray_action.py", "artifacts/qq-system-diagnosis-20261118-7ONesT4M/report.md",
         "artifacts/kos-tray-update-20261118-3JiFAcY0/report.md",
         "artifacts/kos-tray-click-20261118-qcSxYAW7/report.md"),
        "先从面板实点并观察 SNI 方法、实参和 QQ 窗口状态。NextKde 的 tray.action 为无 DBusMenu 项转发点击。"
        "KDE XEmbed 代理需要实际指针位置。从 KWin 取得逻辑坐标，按 XRandR 输出几何换算到 XWayland 像素，再向代理发送一次。"
        "现有 DBusMenu 沿用菜单路径。"
        "旧 Meeting 启用了 Portal 总线代理，新日用未启用；两版代理允许 org.kde.StatusNotifierWatcher 的规则相同。"
        "旧托盘的注册方式缺少现场记录。当前日用 KOS 已实点验证左键恢复同一个 QQ 窗口，右键打开原生菜单。",
    ),
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symptom", nargs="*", help="symptom keywords, e.g. 'Wayland socket' or '输入法'")
    parser.add_argument("--list", action="store_true", help="show a compact list")
    parser.add_argument("--release", metavar="VERSION", help="filter by an exact release, with optional v prefix")
    parser.add_argument("--json", action="store_true", help="output the matching records as JSON")
    args = parser.parse_args(argv)

    release = args.release.casefold().removeprefix("v") if args.release else None
    if release in {"0.0.7dev", "0.0.7.dev0"}:
        release = "0.0.7"
    words = " ".join(args.symptom).casefold().split()
    matches = []
    for repair in REPAIRS:
        if release and repair.release.casefold() != release:
            continue
        searchable = " ".join((
            repair.title, repair.symptom, repair.release, *repair.source, repair.applicability,
        )).casefold()
        if all(word in searchable for word in words):
            matches.append(repair)

    if args.json:
        print(json.dumps([asdict(repair) for repair in matches], ensure_ascii=False, indent=2))
    elif not matches:
        print("没有匹配的补丁。用 --list 查看可检索条目。")
    else:
        compact = args.list or (not release and not words)
        for repair in matches:
            print(f"[{repair.release}] {repair.title}")
            if not compact:
                print(f"  症状  {repair.symptom}")
                print("  源码  " + " · ".join(repair.source))
                print("  证据  " + " · ".join(repair.evidence))
                print(f"  判断  {repair.applicability}\n")
    return 0 if matches else 1


if __name__ == "__main__":
    raise SystemExit(main())
