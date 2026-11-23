# Wayland 挂载热修

日用入口已切到 `0.0.7+wayland.1`。补丁基于正式 v0.0.7 独立构建，开发树已同步修复。正式 v0.0.7 仍可回退。

14:47、14:48 从桌面尝试安装 QQ 时，Debian 13 日用 `meeting` 实例尝试绑定旧 `wayland-0`。宿主会话已使用 `wayland-1`，crun 在原生包名读取前因旧路径缺失退出。

停止态启动现在同时核对注册记录和 Podman 实际挂载，验证当前 socket 的所有权和 inode，通过仅本次调用启用的 Podman 模块修改 OCI 挂载源。客体路径、Wayland 环境变量和其他挂载沿用原值。

- 由原 v0.0.7 归档应用最小补丁。补丁包重新解包后 1067 项离线检查通过，5 项条件跳过。[回归记录](unifier-0.0.7+wayland.1.tar.gz.validation.log)
- 开发树全量离线回归 1149 项通过，5 项条件跳过。[回归记录](../wayland-refresh-full-final-20261116T150304402130319/command.log)
- 固定原完整容器 ID 只读预检。Podman 接受临时模块，使用实际挂载生成的测试输入只替换 `wayland-0` 的宿主源。[预检](preflight.log)
- 全局入口及 FreeCAD、会议各两条动态包装均返回补丁版本。已有实例仍为 exited、PID 0。日用导入记录、注册和 recovery-required 租约文件的哈希保持一致。[部署](deployment.json)
- QQ 安装需要从 KDE 原入口复试。

回退时运行 `python3 -B deploy.py --restore`。脚本使用既有部署锁，只有当前入口仍精确指向本补丁时才切回 v0.0.7。
