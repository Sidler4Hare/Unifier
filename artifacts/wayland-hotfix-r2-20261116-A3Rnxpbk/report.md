# Wayland 重启补丁

15:09 的 QQ 安装重试通过了首次启动和 `dpkg-deb` 包名探测，识别为 `linuxqq`。容器随后停止。正式安装事务在第二次启动遇到旧 Wayland 源，失败于 `runtime` 阶段。原生包管理命令列表为空，事务无需恢复。

Podman inspect 使用最近生成的 OCI spec，因此停止后仍可能报告第一次启动已替换的 Wayland 源。第二次启动会根据原始配置重新生成 spec。第二版补丁识别同一私有运行目录中编号变更的 Wayland bind，继续以已登记的客体路径、完整 CID 和 OCI 钩子中的原始挂载校验为前提。

- 新增的回归覆盖读取包名、停止、再次启动、写入并核对包清单。两次启动均验证相同客体路径和完整容器 ID。
- `0.0.7+wayland.2` 独立解包回归 1067 项通过，5 项条件跳过。使用真实 Podman inspect 和当前挂载完成只读预检。
- 日用入口已切换。原容器、采用登记、租约和失败事务记录保留。真实 QQ 安装待原入口复试。

[原事务](retry-transaction.json) · [定向回归](build-t1/command.log) · [解包回归](unifier-0.0.7+wayland.2.tar.gz.validation.log) · [部署](deployment.json)

回退到上一版日用补丁可在本回执目录执行 `python3 -B deploy.py --restore`。
