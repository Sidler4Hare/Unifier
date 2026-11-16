# 专用 DRM 实例的 EGL 依赖准备

| 实例 | 六个直接请求 |
| --- | --- |
| Intel Debian 13 | `python3 libgbm1 libegl1 libegl-mesa0 libgles2 libgl1-mesa-dri` |
| AMD Fedora 44 | `python3 mesa-libgbm libglvnd-egl mesa-libEGL libglvnd-gles mesa-dri-drivers` |

通过 `hardware_test_guard.py run` 执行各次独立的 `phase=prepare`。spec 的 `command` 以 `['/usr/bin/python3', '<package-controller.py 的绝对路径>', ...]` 开头；每次使用新回执目录、不同 unit、同一已登记 CID。AMD 各次 `prepare` 继续要求有效的 root ACL unit 和足够剩余窗口。控制器通过正式 `service.start` 启动一次，并在 `finally` 中调用正式 `service.stop`。各阶段由主控检查保护器回执和停止态。

## 查询和审查

- `capabilities` 查询现场原生命令的版本和帮助。Fedora 顺序为 `/usr/bin/dnf --version`、`/usr/bin/dnf --help`、`/usr/bin/dnf repoquery --help`、`/usr/bin/dnf repo list --help` 和 `/usr/bin/dnf install --help`。先确认现场版本支持 `--cacheonly`、`--dump-repo-config`、`--assumeno` 和 `repoquery --queryformat`。
- `query` 使用既有缓存读取原生候选、来源和完整求解闭包，不下载软件包。Debian 记录原生 APT 配置、Debian keyring、`apt-cache policy`、无删除模拟以及 `apt-cache show` 的版本、架构、SHA-256、下载和安装量。Fedora 读取有效仓库配置与 RPM 包签名策略，通过 `--cacheonly --assumeno install` 获取求解表，用精确 NEVRA 对应 `repoquery` 的字节字段。现场格式出现未知操作、仓库来源不明或缺少相应记录时，保护内查询停止并保存原始证据。 [Debian APT 缓存](https://manpages.debian.org/trixie/apt/apt-cache.8.en.html) · [Debian APT 模拟](https://manpages.debian.org/trixie/apt/apt-get.8.en.html) · [DNF5 命令选项](https://dnf5.readthedocs.io/en/latest/dnf5.8.html) · [DNF5 Repoquery](https://dnf5.readthedocs.io/en/stable/commands/repoquery.8.html)
- `metadata-refresh PRIOR_DOWNLOAD_BYTES` 是单独的元数据准备阶段，`PRIOR_DOWNLOAD_BYTES` 来自当轮累计下载台账。Debian 要求已核对 Debian keyring、来源以及 `deb.debian.org` 的单一 HTTP 或 HTTPS 传输。APT 使用原生 `Dl-Limit`、直连、无流水线、无重定向和零重试。限速不超过 512 KiB/s，预算计算采用 90 秒，并将剩余预算至少一半留给传输与计量开销。客体 `timeout` 在 85 秒时终止，最多再给 3 秒，外层 90 秒。单独保留缓存前后表、guest RX 和累计值。Fedora 的聚合元数据和 metalink 字节上限仍待证明，入口会在发起下载前返回 `BLOCKED`。 [APT HTTP 限速及并发](https://manpages.debian.org/trixie/apt/apt-transport-http.1.en.html) · [APT HTTPS 选项](https://manpages.debian.org/trixie/apt/apt-transport-https.1.en.html)

主控用 `query/result.json` 的 `plan.package_plan` 生成安装 spec 中 `package_plan` 的四个字段。`requests` 只含上表六个版本锁定的直接请求，`plan.closure` 保留所有变更依赖及其元数据字节/来源。各发行版默认 recommends/weak dependencies 纳入模拟，以吻合当前正式 native argv。APT `Installed-Size` 转换为字节，DNF5 的字节数据以精确 RPM 查询值为准。安装体积按入站包合计，不抵扣替换掉的旧版本。如果所有依赖已经满足且闭包为空，零输入的结果只用于证明无需启动包事务。

## 正式安装

在审查当前原生输出、签名配置、完整求解闭包以及累计资源预算后，控制器参数为以下五项。

```text
install
/absolute/path/to/reviewed-query/result.json
PRIOR_DOWNLOAD_BYTES
PRIOR_WRITTEN_BYTES
INSTALL_METADATA_RESERVE_BYTES
```

第一项为模式。第二项是同 CID 的宿主私有查询回执。后三项使用本轮实际累计下载、累计写入和即将发生的原生元数据请求预留字节。Debian 的正式事务固定会先 `apt-get update`，DNF5 正式事务也可能刷新过期缓存，均计入预留。控制器提前拒绝累计预计下载超过 200 MiB，或累计写入加上包缓存、安装内容和元数据预留超过 1 GiB。许可还需主控核对监督器磁盘保护与累计网络预算，预留须来自现场记录。原生更新后仓库或依赖闭包仍可能改变，整个实际下载量应由主控保留独立证据。

控制器在同一次正式启动内重新执行缓存查询，要求直接版本、完整闭包与来源逐项吻合。实际写入只调用 `service.install_packages(env_id, basenames)`，版本锁定由硬件保护器注入。完成后保存 `package-transaction.json`、检查事务完成且无恢复要求，并以原生已安装查询核对直接依赖和闭包的精确版本。最终对照安装阶段实际 guest RX 与前序累计台账。查询产生的原生 stdout/stderr 按命令编号保存，每流最多 512 KiB，超出标为证据不足并停止。正式事务保留产品的有界 native 输出和原生状态。

脚本仅做过静态核对。Fedora 求解器的 C locale 表格及仓库 dump 须先与现场回执核对。T2 运行、实际传输和正式包事务由主控执行。
