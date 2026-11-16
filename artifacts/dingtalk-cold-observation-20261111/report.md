# 冷启动观察：容器启动前失败

记录器启动前确认旧主进程及子进程已退出，日常 CID `b9dd15c5fccf9f02a1033d6c824066563713fb3f8cfe5bea37b73378417cefdb` 为 exited。用户手动启动后没有新的钉钉主进程，未到首次上传动作。

根因：容器既有 bind mount Source/Destination 都固定为 `/run/user/1000/wayland-0`。本轮宿主只有 UID 1000 的 `wayland-1` socket，wayland-0 不存在。journal 的明确错误为 crun 无法 stat wayland-0，导致 podman start 失败，见 startup-error.log、socket-state.json。这不是本轮测试保护触发，也不能算钉钉 CEF 探测失败。

现有租约为空、bridge=null。当前 podman update 帮助只提供资源、环境和 healthcheck 更新，未提供 mount 修改；不能通过只改 Unifier 的 JSON 记录来改变已创建容器的固定挂载。

## 待授权的最小临时恢复

本方案只新增 `/run/user/1000/wayland-0 -> wayland-1` 符号链接。执行前重新核验 wayland-0 不存在（包括断链）、wayland-1 是当前用户拥有的 Unix socket、父目录属当前用户。创建使用不覆盖已有目标的 symlink 操作。记录创建链接的 lstat 身份和目标。

此操作不移动或替换 wayland-1，不重启 KWin/桌面，不调整显示变量，不修改容器数据库，不创建容器、不复制账号数据。随后由用户从原入口启动钉钉并打开上传框；观察器继续只读取证。

回退仅删除本次创建、再次核验 inode/UID/目标一致的链接，绝不删除 socket。链接位于用户 runtime 目录，通常随会话/重启清理；它是旧容器路径的临时兼容措施，不是 Unifier 处理会话 socket 变化的永久修复。若删掉链接，下一次冷启动仍可能因原挂载路径缺失失败；是否保留要明确告知用户。

该路径属于宿主会话目录，依项目测试规范第 8 节需对这项具体变更获得明确授权；当前尚未创建链接。历史 GUI/PRE03 仍不因人工观察而视为通过。
