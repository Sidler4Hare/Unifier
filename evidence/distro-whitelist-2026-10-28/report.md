# 发行版派系白名单验证报告

日期：2026-10-28，Asia/Shanghai。工作分支：`codex/unifier-v2-control`。

结论：**白名单策略、产品入口接入和 Fedora 本机三客体基础复验通过；其他真实宿主尚未验收。**

## 实现范围

默认 `compatibility.mode=whitelist`。18 个明确宿主 ID 分为 Debian/Ubuntu、Fedora、RHEL、Arch、SUSE 五组。Alpine/NixOS/Void/Gentoo 可识别但暂不准入；未知 ID 不凭 ID_LIKE 继承资格，Atomic/OSTree/事务式、非 Linux x86_64 或未观察到 glibc 的宿主拒绝。

新增 `compat list/check/matrix`，在 `doctor` 中显示派系准入和三目标关系；创建、启动、直接执行、原生包查询与包事务统一检查当前宿主。应用/文件路由的绑定预检、项目会话环境预检也检查策略，避免先创建 supervisor/portal bridge 再发现宿主被拒绝。环境记录保存创建时关系，运行时重新检查；同派系不混用包数据库、系统目录、镜像身份或 UUID/代际/CID 授权。

宿主候选扩展不增加客体适配器；客体仍是 digest 锁定的 Debian 13、Fedora 44 和 Arch。完整分类、矩阵及官方资料见[设计说明](../../设计/发行版派系白名单与容器关系-2026-10-28.md)。

## 验证结果

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| 新增契约测试 | 20 项通过，含全部 ID × 三目标子例、未知/Atomic/错平台拒绝、原有客体身份检查、旧记录、应用与项目预检 | [测试源码](../../tests/test_compatibility.py)、[最终完整日志](tests-final2.log) |
| 完整回归 | 443 项，439 通过，4 项条件跳过 | [最终完整日志](tests-final2.log) |
| 当前 Fedora 44 KDE 宿主预检 | 通过；whitelist、Python、Linux amd64、Podman、Skopeo、rootless、cgroup v2 均通过 | [doctor](doctor-final.json) |
| 当前宿主关系 | Debian/Arch 为跨派系；Fedora 为同发行版；均是 policy-only 候选 | [关系矩阵](host-matrix-final.json) |
| 最终源码真实三客体基础链路 | 3/3；逐个创建、启动、执行、原生包查询、停止和删除通过 | [最终本机回执](live-final.json) |
| 真实容器上的未知宿主输入回放 | 3/3 拒绝；这项使用模拟 host facts，不是另一宿主实测 | [最终本机回执](live-final.json) |
| 原有日常容器保全 | 前后 CID、名称、状态、StartedAt、FinishedAt 相同；只剩原两个容器 | [最终本机回执](live-final.json) 的 before/after |
| 修改前文件恢复备份 | 9 文件逐一 SHA-256 校验通过 | `.lab/backups/distro-whitelist-20261028-200931/manifest.json` |
| 最终运行源码和测试版本 | 已固定 SHA-256，便于复核 | [源码清单](source-manifest.json) |

本机三客体使用既有缓存镜像，无拉取、安装或升级包。创建过程中原生发行版 ID/版本、原生包清单和共享内核由产品检查；运行阶段检查 `true`、退出码 17，以及 Debian `base-files` / Fedora、Arch `filesystem` 原生查询。每个测试环境使用独立临时 registry，按自身项目标签和 CID 删除，不清理或启动既有日常环境。

最终本机回执 `passed=true`、`errors=[]`、`existing_containers_preserved=true`。第一次真实链路[回执](live-results.json)和中间全量日志保留，最终结果以上表指定版本为准。

## 复现

```bash
python3 -m unittest discover -s tests -v
python3 tools/unifier.py compat list
python3 tools/unifier.py compat check --target debian13
python3 tools/unifier.py compat matrix
python3 tools/lab.py doctor
python3 evidence/distro-whitelist-2026-10-28/live_probe.py --output /tmp/new-whitelist-result.json
```

`live_probe.py` 要求全部镜像已缓存，拒绝覆盖已有回执；使用当前真实宿主，结束后删除自身临时环境。只复现本报告的基础范围。

## 边界与交付状态

- 五组宿主策略准入是工程政策，不能写成五组真实机器都已通过。Ubuntu、Mint、RHEL、SUSE、Arch 宿主等仍需独立验证内核、Podman/cgroup、安全政策、桌面和设备。
- 不新增宿主包管理/全局命令接管；不自动把同派系容器操作改成宿主操作，不因为同为 DEB/RPM 就接受任意安装包。
- GUI/真实会议/媒体、跨宿主、长时间压力和既有 FUSE 开放关口不在本次验收范围。
- 当前改动在工作区。两个日常部署副本未同步，未提交/推送；已有其他修改保留。本轮未 reset、clean、重建日常容器或修改默认应用。
