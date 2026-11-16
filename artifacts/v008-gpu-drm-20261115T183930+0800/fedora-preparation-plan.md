# AMD Fedora 44 的依赖查询与下载预算

**状态为 BLOCKED。** 新实例 CID `2180a83540510175c2d177c0decf0cf852788fbe77d6c6263d98737fd644fa3f` 已创建并停止，当前缺少可供 `prepare` 使用的正式环境绑定。待主控按现有创建证据恢复登记并核对归属、镜像、设备、停止态和活动 ACL 看门狗。保持 guest 384 MiB、1 CPU 及原保护器和阶段期限。旧 Fedora CID `31324dbcefe5262532f4e8ce8d4bf7220a2a31ed8d5cc00f9e747b413d194156` 只读保留，禁止启动它为新实例补缓存。

已登记的服务器镜像盘点含 Image ID 和容器状态，未包含基础镜像的 RPM 清单。旧 Fedora CID 的 154 项历史原生清单包含 `python3 3.14.10-1.fc44.x86_64`，其中查不到 EGL 探针所需的 `mesa-libgbm`、`libglvnd-egl`、`mesa-libEGL`、`libglvnd-gles`、`mesa-dri-drivers`，也没有 `r600_dri.so` 文件证据。旧 CID 的 Python 版本不能证明新 CID 已安装 Python。 [服务器盘点](server-before-inventory.json) · [旧 Fedora 原生清单](../pkg-recover-journals-20261114T135636+0800-dnD9ha9r/ubuntu/fedora44/recovered-native.txt)

## 最小原生查询路径

1. 重用旧实例停止态上层 `libdnf5` 中上一回执列出的 12 个原始文件，共 49,794,425 B。`fedora`、`updates`、`fedora-cisco-openh264` 各取 `metalink.xml`、`repomd.xml`、对应 `primary`，并保留清单内的 comps 和 updateinfo，兼容当前默认可选元数据。排除 `.solv`、`.solvx`、旧临时测试 repo 和其他文件。先在服务器 T1 保护中对清单逐文件确认 CID、`UpperDir`、owner、regular/nonsymlink、大小和前后 stat，并流式计算 SHA-256。旧缓存无 RPM。DNF5 可重建派生求解缓存。 [旧缓存精确清单](fedora-cache-review.md) · [DNF5 缓存格式](https://dnf5.readthedocs.io/en/stable/misc/caching.7.html)
2. 在正式装置 `capabilities` 回执确认 DNF5 版本、`--cacheonly`、`--assumeno`、`repoquery --queryformat`、`--dump-repo-config`。用 `--cacheonly --dump-main-config` 和 `--cacheonly --dump-repo-config=*` 确认目标 `system_cachedir`、实际 enabled repo、Fedora 44 x86_64 来源和 `pkg_gpgcheck=1`。将目标实际 repo 配置与缓存 key 对照；猜测 key、跳过缺失 repo 或替换发行版均会改变求解输入。将客体已有的签名密钥指纹与 Fedora 44 官方 `36F6 12DC F27F 7D1A 48A8 35E4 DBFC F71C 6D9F 90A6` 核对。 [Fedora 官方签名密钥](https://fedoraproject.org/security/) · [DNF5 配置参考](https://dnf5.readthedocs.io/en/stable/dnf5.conf.5.html)
3. 用可信发布源认证 `repomd.xml`。若 repo 本身提供可验证的签名，遵守原有 `repo_gpgcheck` 和密钥策略。否则有限获取该有效 repo 配置指向的 Fedora 官方 HTTPS Metalink，将其中**当前** `repomd.xml` 的强摘要与旧副本一致性作为复用条件。历史 Metalink 与历史 repomd 的互相匹配仅提供内部一致性。无当前可信摘要、摘要变化、签名未知时阻断相应 repo。对认证的 repomd，逐个核对 `primary`、comps、updateinfo 的相对位置、Size 和强摘要，同时检验旧 Metalink 指向同一已核验快照；不接受目录逃逸、绝对路径、远程 `xml:base` 或对象重复。 [DNF5 Metalink/repomd 实现](https://github.com/rpm-software-management/dnf5/blob/main/libdnf5/repo/repo_downloader.cpp)
4. 用已审查的固定文件写入步骤，将这 12 个文件通过批准的本轮私有 share 送达**新 CID**对应的 DNF 缓存路径，回读 SHA-256。现有 `HardwareRuntime.copy_in` 只接受 DRM 探针，因此缓存准备需独立、可审核的专用写入步骤。不得以通用 `copy_in` 或挂载旧完整上层代替。旧源维持只读，staging 与正式写入均纳入磁盘和进程树预算。
5. 在另一次 `phase=prepare`、无 `package_plan` 的 spec 调用 `package-controller.py query`。该入口使用 `dnf --cacheonly` 即缓存全部模式，对六个固定直接请求运行 `--assumeno install`，再用 `repoquery --queryformat` 给求解出的精确 NEVRA 填 repo、location、下载字节和安装字节。原生仓库、solver 表格和字节记录须完整；保存请求、完整闭包和 `plan.package_plan`。核对新实例有效插件没有额外联网路径，并保留网络计数从而确认本次查询没有外部元数据请求。元数据缺失、DNF5 因未知格式失败或缓存求解触及 384 MiB 时阻断当前方案，保留原回执。 [现有控制器说明](package-notes.md) · [DNF5 选项](https://dnf5.readthedocs.io/en/latest/dnf5.8.html) · [Repoquery 字段](https://dnf5.readthedocs.io/en/stable/commands/repoquery.8.html)

## 200 MiB 累计下载与正式事务

查询阶段只消费经认证的既有元数据。认证如需取新 Metalink，最多 3 个固定 HTTPS URL，每个响应体上限 128 KiB，共最多 393,216 B。只能从现场有效 repo 配置取 URL，须审核主机、发行版和架构。独立受保护地串行获取，curl 8.4.0 及以上使用 `--max-filesize 131072 --max-time 10`，关闭用户 curl 配置、代理、重定向、自动重试和自动解压。保存 HTTP 状态、证书验证结果、有效 URL 和实际字节。实际下载前记录累计已有下载及协议开销预算。超限、无官方强摘要、网络来源不可证即停止，禁止改用 `dnf makecache` 自动刷新大索引。 [curl 传输限制](https://curl.se/docs/manpage.html#--max-filesize)

正式包事务如果要求下载体积能在执行前逐项封顶，先由已认证的 `primary` 或预装的 libdnf5 API 为完整闭包取每包 NEVRA、仓库、路径、精确长度和强摘要，逐项核对原生查询表。`repoquery` 的当前字段不提供包 SHA-256；可用设备上现有的 libdnf5 `get_checksum()`，或在保护内以有解压输出上限的 `primary` 流式解析提取。没有经过验证的读取器或对应 checksum 则保持待定。含 `xml:base`、路径逃逸、缺少固定 Repo URL、镜像版本漂移或 RPM 404 都会阻断。 [libdnf5 包摘要 API](https://dnf5.readthedocs.io/en/stable/api/python/libdnf5_rpm.html#libdnf5.rpm.Package.get_checksum)

可审查的后续执行方式是通过批准的直连 HTTPS 源逐包串行预取，而后在完全缓存模式走正式安装。

- 开始前对 `D_prev + D_meta + Σ(RPM 压缩字节) + R_wire ≤ 209,715,200` 作逐项核对。`D_prev` 包括本轮失败/中止下载，`D_meta` 包括 Metalink 或签名获取，`R_wire` 由当轮独立准入确定。下载体积再与缓存、重复 staging、生成的求解缓存、原生安装文件一起核对累计 1 GiB 写入预算。
- 只给已核准的固定 URL 和同一 repo 的可信镜像基址发请求，串行单次，关闭自动重试和重定向。每个 RPM 以已认证精确长度设 `--max-filesize` 并增加统一绝对期限，保存结果到私有临时文件。长度、强摘要和 Fedora 签名均匹配才标为可用。失败的已接收字节照计上限。签名核对保持 Fedora 44 可信密钥和 `pkg_gpgcheck`。对网络线速或总线字节要求硬保证时，该方案还须有独立流量保护证据，应用层长度上限与事后 RX 计数各有自己的口径。 [curl 最大文件大小](https://curl.se/docs/manpage.html#--max-filesize) · [RPM 签名核验](https://rpm.org/docs/6.0.x/man/rpmkeys.8)
- 将查验通过的 RPM 放到**新实例**对应 repo cache key 下 `packages/<basename>`，由 DNF 本身再校验长度和摘要。正式调用依旧是 `service.install_packages`，其实际 `dnf install -y` 须在本轮专用配置使用 `cacheonly=all`，并重复验证同一 repo 配置与完整闭包。缺文件时 DNF 在该模式拒绝网络补包。现有 guard 只注入版本锁定，并无这段预取、staging、缓存模式门禁。应先审查实施条件，当前保持未运行。`cacheonly=metadata` 可固定 repo metadata，却允许包下载器发起额外 Metalink 请求，也缺少总下载上限的证明。 [DNF5 原生包缓存检验](https://github.com/rpm-software-management/dnf5/blob/main/libdnf5/rpm/package.cpp) · [DNF5 下载路径](https://github.com/rpm-software-management/dnf5/blob/main/libdnf5/repo/package_downloader.cpp)

本轮仅完成只读设计和上游语义核对。旧缓存尚未 hash 或导入，AMD 查询和正式包事务均未执行。


后续实测进展以 [DRM 总回执](report.md) 为准：已完成同 CID 正式登记、离线闭包查询、28 包认证预取和原生 RPM 签名核验；本文件早期 BLOCKED / 未执行描述保留当时含义，不作为当前结论。
