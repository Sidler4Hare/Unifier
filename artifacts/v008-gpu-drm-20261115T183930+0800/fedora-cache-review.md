# 服务器 Fedora 44 旧缓存只读核对

2026-11-15 19:19 CST，通过 `unifier-server` 核对既有实例 `ml-2b804374-fedora44`。完整 CID `31324dbcefe5262532f4e8ce8d4bf7220a2a31ed8d5cc00f9e747b413d194156`，状态 `exited`、`Running=false`、`Pid=0`。镜像 ID `4b5d9a75c81b96b542ac08b19587efa516da72bf02581908c6178d39de4bb07b`，清单摘要 `sha256:10667e171df877cedd245b7065238143e09eaae7da4656d3e62eb7c094378011`，与批准的新 Fedora 基础镜像一致。

只遍历 Podman 给出的 `UpperDir` 下的缓存和仓库配置路径，累计 38 个条目、19 个子目录，未触发 300 条目或深度 5 限额。`/var/cache/libdnf5` 有 19 个常规文件，表观 111,970,397 B（106.78 MiB），分配 112,037,888 B。没有 RPM，也没有六个 EGL 直接依赖的安装文件。`/var/cache/dnf`、`/etc/dnf/dnf.conf` 和 `/etc/yum.repos.d` 在该上层均不存在。旧容器的下层镜像及有效仓库配置未读取。缓存原始文件未打开读取、计算哈希或复制。

## DNF5 可用边界

DNF5 的 `--cacheonly` 和 `-C` 无值，均固定为 `all`。在原生命令显式选择 `--setopt=cacheonly=metadata` 后，仓库对象使用 `ONLY_CACHE`，同时包下载器仍可下载 RPM。只在 `all` 模式下拒绝下载缺少的 RPM。可在新 CID 先用 `/usr/bin/dnf --setopt=cacheonly=metadata --dump-main-config` 回读配置。`skip_if_unavailable` 用来跳过打不开的仓库，可能隐藏闭包缺失。`metadata_expire=-1` 只取消年龄过期，仍有配置文件时间变化和缓存缺失等路径。两者均不能替代 `cacheonly=metadata`。 [DNF5 CLI 源码](https://github.com/rpm-software-management/dnf5/blob/main/dnf5/main.cpp) · [仓库同步策略](https://github.com/rpm-software-management/dnf5/blob/main/libdnf5/repo/repo.cpp) · [包下载器](https://github.com/rpm-software-management/dnf5/blob/main/libdnf5/repo/package_downloader.cpp) · [配置参考](https://dnf5.readthedocs.io/en/stable/dnf5.conf.5.html)

原始 `repomd.xml` 和 `primary` 是常规包查询的必要输入。`solv` 和 `solvx` 为本地派生缓存，可由相同 DNF5 工具链重新生成。清单保留现有的可选 comps 和 updateinfo，兼容默认配置；如果现场解析还要求当前没有保存的元数据类型，缓存查询失败即停。先用 `--cacheonly` 做闭包模拟，现场验证新 CID 的缓存目录与有效 repo 配置相符。 [DNF5 缓存结构与元数据类型](https://dnf5.readthedocs.io/en/stable/misc/caching.7.html)

原生 RPM 下载器仍会初始化远程 librepo handle。上游源码明确指出，librepo 可能额外下载 metalink。`cacheonly=metadata` 可锁定仓库求解元数据；RPM 下载阶段的镜像发现仍须计入累计网络预算。设定 `baseurl` 并清空 `metalink`、`mirrorlist` 会走直接 URL 路径，属于后续候选，须先确认准确的官方 URL、实际缓存路径与 librepo 的字节限制。现有正式安装 guard 只注入 NEVRA；实机前还须确认 `cacheonly=metadata` 通过新实例专用配置或经过审查的正式调用传给原生 DNF5。 [DNF5 远程句柄与 metalink](https://github.com/rpm-software-management/dnf5/blob/main/libdnf5/repo/repo_downloader.cpp)

## 原始缓存候选清单

以下均相对于源前缀 `/home/agent/.local/share/containers/storage/overlay/3ed049d410f34838c8bc1e517d9da720610524131ce88396fd816455c9d7fa59/diff/var/cache/libdnf5/`。已保留当前观察到的三个原生 repo 目录中的全部原始缓存。总计 **49,794,425 B（47.49 MiB）**，低于 120 MiB。排除 62,175,972 B 派生求解器缓存以及旧临时 `unifierfixture` 的 0 B `repomd.xml`。

| 源相对路径 | 字节 | SHA-256 |
| --- | ---: | --- |
| `fedora-cff72538bc9825a4/metalink.xml` | 21,274 | 待保护内计算 |
| `fedora-cff72538bc9825a4/repodata/repomd.xml` | 5,966 | 待保护内计算 |
| `fedora-cff72538bc9825a4/repodata/e32a0c328ac8716e71e3f66e87366a172fea8ecb2f452909a42db9c080cd3704-primary.xml.zck` | 36,915,318 | 待保护内计算 |
| `fedora-cff72538bc9825a4/repodata/6a37d36914955dc65cf6dc037c37dcf00c64ded464dc34164a674bf65fa16927-comps-Everything.x86_64.xml.zst` | 291,172 | 待保护内计算 |
| `updates-3cc07c89a20302f2/metalink.xml` | 15,659 | 待保护内计算 |
| `updates-3cc07c89a20302f2/repodata/repomd.xml` | 7,055 | 待保护内计算 |
| `updates-3cc07c89a20302f2/repodata/8f64356257b052f4db5c815a935b4e7377c2088c2e90801edd70f1affc90237b-primary.xml.zck` | 9,251,141 | 待保护内计算 |
| `updates-3cc07c89a20302f2/repodata/e43af6c409ab4a1552a595ffae9bd8cd72cf4b059180da53b2e1a440d694db53-comps-Everything.x86_64.xml.zst` | 295,349 | 待保护内计算 |
| `updates-3cc07c89a20302f2/repodata/b5baede7a090ad551f326d9ac58c3f21551dc6378ad848195561d0c179b06c81-updateinfo.xml.zck` | 2,986,086 | 待保护内计算 |
| `fedora-cisco-openh264-d770e872c130a03f/metalink.xml` | 986 | 待保护内计算 |
| `fedora-cisco-openh264-d770e872c130a03f/repodata/repomd.xml` | 3,077 | 待保护内计算 |
| `fedora-cisco-openh264-d770e872c130a03f/repodata/ef464269998980f44c8b2c94cf023d2343751c9c2325e8197a457b378f451226-primary.xml.zst` | 1,342 | 待保护内计算 |

## 来源验证和复用条件

1. 在服务器串行 T1 保护内核对同一已停 CID、`UpperDir`、文件类型、解析路径、设备号及大小。按上表固定文件清单逐个打开，拒绝符号链接，使用单线程小块流式读取计算 SHA-256，核对读取前后 inode、大小、修改时间。将记录写入新私有回执。全程只读旧上层，不遍历或复制其它文件。
2. 用新 CID 实际解析的 Fedora 44 x86_64 repo 配置核对 repo ID、缓存键、官方 HTTPS metalink 地址、签名策略及固定 Fedora 44 签名密钥。在独立有界的验证中，为每个候选 `repomd.xml` 核对可信官方 HTTPS metalink 对当前 repomd 公布的强摘要，或在官方配置和签名均可验证时用发布方签名核对。旧缓存中的 metalink 与 repomd 自洽只能证明内部一致。官方摘要缺失、发布新索引后不再认可旧 repomd，或签名策略无法确认时将该 repo 标作阻断。以验证通过的 repomd 为锚核对各输入的相对路径、压缩文件字节数及强摘要。
3. 主控批准后，将有限集合写入专用新 CID 的私有缓存，按 SHA-256 回读复核。运行完全缓存的原生闭包查询核对包版本、来源和完整规模。正式安装保持有效 `pkg_gpgcheck`，从新 CID 可信发行版密钥核验下载的 RPM，签名或内容摘要不符则停止。只有完成这些核对才允许使用缓存；现有证据支持**元数据有条件复用**，RPM 离线复用无来源。
