# v0.0.4 复跑与日常入口切换

结果：源码包已生成，日常入口已切到 v0.0.4。这不是 PRE03、同管理根多环境、Arch/RPM 清单、通用单实例或私有 GUI 的验收。双击 `.deb` 的默认程序没有改。

## 包

- `artifacts/unifier-0.0.4.tar.gz`
- SHA-256：`b04db4836d799f76369912ab68e175d0c308a5ee5c78f69191f50374579f9462`
- 264 个源码/文档条目。独立解包检查 `Ran 846 tests in 62.878s`，`OK (skipped=5)`。
- 代码目录：`~/.local/share/unifier-code/releases/0.0.4-b04db4836d79`
- 0.0.3 目录 `~/.local/share/unifier-code/releases/0.0.3-892b1623c220` 仍在。

12:26 的尝试因宿主 memory PSI `full avg10` 达到 4.87 被停下。这次按同一限额复跑：准入 30 秒，最低可用内存 4819116 KiB，压力 avg10 为 0。实际 cgroup 为 `memory.max=536870912`、`pids.max=96`、`cpu.max=100000 100000`、swap 0。运行中 cgroup 最高约 194 MiB，宿主压力 avg10 保持 0。

安装目录里的 `docs/v0.0.4.md` 和 `HANDOFF.md` 是打包时的文字，还没有写上最终校验和。工作树在切换后补上了目录名和校验和。

## 入口

`~/.local/bin/unifier` 指向新目录的 `bin/unifier`。`unifier --version` 为 `Unifier 0.0.4`。

回退命令只恢复命令入口：

```bash
mv -Tf -- /home/omg/.local/bin/.unifier-e_irnbfl.rollback /home/omg/.local/bin/unifier
```

该链接和 `~/.local/bin/.unifier-0.0.3.rollback` 都指向 0.0.3 的 `bin/unifier`。用其中一条执行上面的移动即可。`.unifier-0.0.2.rollback` 没动。回退包装时，把下面四个文件以及安装管理桌面项、`package-offer.json` 的 `launcher` 改回 `.../releases/0.0.3-892b1623c220`。

FreeCAD 与腾讯会议的 `tools/ml.py`、`tools/unifier.py` 只改了 `RELEASE`。`APP_ROOT` 仍是各自的应用目录。安装管理桌面项的 `Exec` 改为新目录的 `bin/unifier offer %f`，没有重跑 `offer --setup`。`package-offer.json` 只改了 `launcher`，原来的 MIME 记录还在。

用户数据 `~/.local/share/unifier` 和两个应用的数据根没有改。没有启动容器，也没有重启这两个应用。
