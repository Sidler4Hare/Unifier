# 2026-11-07 正式安装用例回执

客体内装 gcc 工具链时，`apt-get install` 在 300 秒产品时限内还在下载，事务失败，`dpkg --audit` 为空，没有留下需要恢复的包库。夹具改在宿主用 glibc 2.34 接口编译，再经正式 `install_local`、`repair_dependencies` 和 `remove_packages` 装进固定 Debian CID。没有新建容器，没有改 Arch 配额，没有动活动桌面。

| 用例 | 结果 |
|---|---|
| DEP01 | PASS。声明依赖 `libestr0` 由 apt 装上，uid 1000 的程序实际加载了 `libestr.so.0.0.0`。 |
| DEP02 | PASS。未声明的 `libfastjson.so.4` 先无法运行，修复后装上 `libfastjson4` 并加载成功。 |
| DEP03 | PASS。扫描包含插件，自动装上 `libconfig11`，插件函数返回 1107。 |
| DEP04 | PASS。第二个程序复用已安装的 `libestr0`，版本未变，容器 ID 未变。 |
| DEP05 | PASS。连续两次 repair 都是 `no-missing-elf-dependencies`，没有新装包。 |
| DEP06 | PASS。只删除 A，B 和 `libestr0` 仍可运行。 |
| DEP08 | PASS。静态扫描没有把运行时拼出的库名当缺失项；`--library libcapstone.so.5` 之后实际加载成功。 |
| SH09 | PASS。两个包事务按秒级时间戳前后相接，心跳从 1 增到 86，容器 ID 未变。 |
| DEP11 | FEATURE_GAP。客体内没有 `gio`，现有修复只处理 ELF，没有自动补外部命令。 |
| DEP07、DEP13、SH10、AR01–AR03 | BLOCKED。没有签名夹具仓库，不在这台唯一容器上打断 dpkg，不用活动桌面跑 Writer，不改 Arch 限额。 |

汇总峰值约 805 MiB，宿主最低可用内存约 4.3 GiB。保护没有触发。容器已停止，租约为空。证据在本目录 `result.json`。
