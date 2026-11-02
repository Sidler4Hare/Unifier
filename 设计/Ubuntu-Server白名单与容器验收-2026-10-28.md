# Ubuntu Server：白名单与无桌面容器验收

本说明用于下一台 Ubuntu Server 的第一次真实宿主验证。当前 Fedora 结果不能提前计作 Ubuntu 通过。先验证宿主准入、容器创建/启动/执行/停机重开；不要求安装桌面、Wayland、X11、摄像头或音频服务，也不启用 `--desktop`、GPU、portal 或文件投影。

2026-10-30 修订：同派系容器禁止，Ubuntu 上只测试 Fedora 44 与 Arch。10 月 28 日便携包和历史说明不能代表当前策略，执行时使用新版源码。

## 准备当前源码

当前便携源码包为 `artifacts/unifier-0.0.1.tar.gz` 及旁边的 SHA-256 文件，包含当前 `src/`、`tools/`、`config/`、`fixtures/`、`tests/`、`bin/` 和说明；不包含本机 `.lab`、容器数据、私有 HOME、登录信息或日常应用安装镜像。10 月 28 日包和三宿主首轮测试包只保留作历史证据。

将源码包和校验文件传到服务器后，在普通用户拥有的新目录里操作：

```bash
sha256sum -c unifier-0.0.1.tar.gz.sha256
tar -xzf unifier-0.0.1.tar.gz
cd unifier-0.0.1
```

这里的内容来自当前工作区，不依赖尚未提交/推送的远端分支。`BUNDLE-MANIFEST.json` 给出逐文件 SHA-256。

## 前置条件

- Linux x86_64/amd64、glibc。初版不会在 ARM64 服务器上偷偷运行 x86 镜像。
- Python 3.11 或更新版本。先看 `python3 --version`；若服务器默认解释器更旧，使用已经准备好的 3.11+ 解释器执行下方命令，不替换系统 Python。
- Podman、Skopeo、可用的 rootless 用户命名空间与 cgroup v2；测试时不使用 `sudo python3` 或 `sudo podman`。
- 当前普通用户有合适的 `/etc/subuid`、`/etc/subgid` 分配及 `newuidmap`、`newgidmap`。分配范围由服务器现有用户配置决定，不直接套用其他机器的编号。

Podman 可从 Ubuntu 官方仓库安装，参见 [Podman 安装说明](https://podman.io/docs/installation)。rootless 身份映射见 [Podman 官方文档](https://docs.podman.io/en/latest/markdown/podman.1.html#rootless-mode)。先盘点已有安装；缺项再按服务器的管理方式补齐。

## 第一步：只读宿主预检

```bash
mkdir -p evidence/ubuntu-server
python3 tools/unifier.py compat check --target fedora44
python3 tools/unifier.py compat matrix > evidence/ubuntu-server/compat-matrix.json
python3 tools/lab.py doctor --output evidence/ubuntu-server/doctor.json
```

应观察到真实 `host.id=ubuntu`、`family=debian`、`observation=live-host`；Ubuntu → Debian 13 为 `same-family` 且 `allowed=false`，→ Fedora/Arch 为 `cross-family` 且准入。`matrix` 因存在准入的跨派系目标返回 0；单独检查 Debian 目标应返回 1。`allowed=true` 只代表策略准入；还要核对 `doctor` 中 rootless、cgroup v2、工具和镜像锁检查。未通过时保留失败输出，先定位具体能力，不把模拟 os-release 文件当作真实宿主通过。

## 第二步：准备准入的锁定镜像

```bash
python3 tools/lab.py pull --output evidence/ubuntu-server/pull.json
```

使用包内既有 digest 锁；首次下载不需要 `lock --refresh`。`pull` 默认只准备宿主准入的 Fedora 44、Arch，不拉取被拒绝的 Debian 目标。

## 第三步：隔离的无桌面验收

```bash
python3 tools/probe_headless.py --output evidence/ubuntu-server/headless-first.json
```

工具要求目标镜像已缓存，不主动拉取、不安装或升级包，拒绝覆盖已有回执。它会创建独立临时 registry，逐个完成：

1. 创建环境，验证真实客体 ID/版本、原生包清单和共享宿主内核。
2. 启动，执行 `true`，确认客体退出码 17 能正确传回。
3. 查询 Debian `base-files`、Fedora/Arch `filesystem`，确认仍使用各自包驱动。
4. 在实际运行的测试容器上回放未知宿主输入，检查拒绝；这只是附加策略反例。
5. 停止后重新启动，确认 CID 不变且还能执行。
6. 停止并精确删除本轮环境；核对既有容器的 CID、状态和启停时间未变。

需要先只测 Fedora 时：

```bash
python3 tools/probe_headless.py --target fedora44 --output evidence/ubuntu-server/fedora-first.json
```

成功回执应为 `passed=true`、`errors=[]`、各目标 `restart_after_stop=true`、`existing_containers_preserved=true`。后续重跑选择新的输出文件名。失败时保留 JSON、报错和工具给出的临时 registry 路径；不要全局 prune 或删除原有容器来消除现场。

## 结果判定

Ubuntu Server 上这些步骤通过后，可以记录“Ubuntu 实机：白名单准入与基础创建/启动/执行/重开通过”。这仍不覆盖 GUI/桌面集成、软件包安装事务、网络、SELinux/AppArmor 的全部配置、媒体或文件投影性能。后续扩展按实际失败和用例推进。
