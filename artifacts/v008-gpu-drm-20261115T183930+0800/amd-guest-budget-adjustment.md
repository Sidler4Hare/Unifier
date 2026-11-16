# AMD Fedora 只读解算查询的客体预算调整

本方案只对应服务器的既有 Fedora 44 DRM 实例。宿主 UID 1001（agent），管理根 `/home/agent/unifier-hardware-v008-drm-20261115a/environment`，完整 CID `2180a83540510175c2d177c0decf0cf852788fbe77d6c6263d98737fd644fa3f`，环境 UUID `a1e18eb1-72f6-4d99-8178-a4c88cb0e213`，generation 1。新装、删除、修改产品默认资源、提高总作业内存或扩大 AMD 设备权限均不在该阶段。

## 读到的原始证据

服务器旧回执位于 `/home/agent/v008-gpu-drm-20261115T183930/amd-package-query`。原 `abort.json` 和 `receipt.json` 为 `GUEST_INVALID_HARD_LIMIT`。作业峰值 504,885,248 bytes，宿主最低可用 118,773,911,552 bytes，旧客体绑定读到 384 MiB RAM、128 MiB swap、128 tasks、1 CPU。12 个通过官方 HTTPS Metalink 和强散列认证的仓库原始缓存文件导入成功，共 49,794,425 bytes。DNF `--cacheonly --assumeno` 解算退出 137，未进入包事务。旧 cleanup/rescue 均核对原 CID `STOPPED`、PID 0，旧正式记录的 `runtime_state=running` 是保护停止未落正式停止记录造成的旧字段。真实状态以原生检查和固定执行时间为准。旧 `State.OOMKilled=false` 只描述容器 init，旧保护器未保存最后的客体计数器，无法据此推断客体各进程均没有 OOM。旧回执和已恢复的 AMD ACL 证据继续保留。

## 一次受保护的调整

Podman 4.9.3 的 `podman update` 路径调用 OCI 运行时更新 cgroup，未把新配置写入 libpod 保存的 OCI spec。这里保留原 384 MiB 配置。只有临时存活的、已验证的目标 scope 可以调整。 [Podman v4.9.3 容器更新实现](https://raw.githubusercontent.com/containers/podman/v4.9.3/libpod/container_internal.go#L2347)

- 新阶段使用 `phase=prepare`、`guest_profile=amd-dnf-retry-768`、`supervisor_tasks=20`、`supervisor_memory_mb=96`，以及新的有限 AMD ACL 服务和唯一 guard unit。command 固定为 `["/usr/bin/python3","-B","/home/agent/v008-gpu-drm-20261115T183930/fedora-query-continuation-inputs/fedora-cache-controller.py","query-continuation"]`，不接受 package_plan 或其它参数。最大 180 秒。
- 初始及启动前检查精确 CID/UUID/generation、原硬限中止、12 文件导入和 solver 137 回执、前后原生库存、旧停止状态、原生引擎和 saved OCI、同一设备以及无事务、无其它启动记录和活动 lease/bridge。独占创建一次重试意图，保留所有旧证据。首次执行使用普通 384 MiB 启动约束，完成 PID/starttime/boot、scope/InvocationID、conmon、aggregate 和实际设备绑定之后，才对该 scope 写入新值。
- 只写该 scope 的 `memory.max=805306368`（768 MiB）和 `memory.high=671088640`（640 MiB）。先登记原有效限额及唯一变更，再回读 exact 异常界线。swap 128 MiB、128 tasks、1 CPU 及祖先链条原样。若 `memory.high` 已有更严限额，拒绝提高。新限额仅随这次活 scope 存在。
- 1 GiB 汇总 cgroup 保持实际硬上限。768 MiB 客体与原 192 MiB 控制器的硬额合计 960 MiB，余 64 MiB 用于同汇总内的外部辅助进程。实际页缓存和进程用量仍由汇总监控拒绝硬限事件。AMD 独立监督器 96 MiB/20 tasks，ACL 服务 16 MiB/4 tasks，宿主实际准入阈值 2,399,141,888 bytes 连续 15 秒、运行保留线 1 GiB。提高额度不能保证 libdnf 本轮峰值一定低于硬限。
- 进程提交前，先看到已登记的精确新有效限额。监督器复核实际 scope 有效限额，按新客体上限运行 95% 持续内存策略并保留 aggregate 规则。若触发客体保护，在抛出前写 `guest-limit-sample.json`，记录原始 memory.events 和 pids.events。停止后核对原 saved OCI 384 MiB/128 MiB swap，原生状态停止，临时 scope 已消失，非目标状态一致，ACL 由独立 root 看守恢复。同类保护再次触发即结束此阶段，保留新标记，不再提额、延长或再试。

## 待主控执行的安装与验收

远端原源目录为 `/home/agent/v008-gpu-drm-20261115T183930/source`。新私有变更目录为 `/home/agent/v008-gpu-drm-20261115T183930/amd-query-memory-update`，应以 0700 创建，在其 `before/tools/` 中保留这三份远端原文件，写入前复核原 SHA，并保存整套新 SHA。只替换下表三份 guard 文件，无需同步产品源码。测试文件只在保护的离线验收中使用。

| 文件 | 原远端 SHA-256 | 新 SHA-256 |
|---|---|---|
| `tools/hardware_test_guard.py` | `0c23634bf00021cba0d31b1b514bc8f0784bd3d4142b0fc9438fa2534adc0aa2` | `1900bd98849e57e94cc714118ef49f027ccf2c0781a745c42f9f78ff1c484617` |
| `tools/hardware_guard_policy.py` | `2b08b117dd396db6d117e962314f44958439b0582c3c550565e91fe68cede2f1` | `a06ea5f244b7adadc6cccbc8d111618d138e3e3f6093db159c46383663b8a40d` |
| `tools/reuse_test_guard.py` | `591d497c875bd48db2ec1d667769d8de1876608f2e22886fdce71546f441483a` | `12c64649339bc3cfe0c057178bc7f51025e8bd00339f3f6ee43b85dc4b96f15b` |

新查询控制器另存于原 RUN 下 `fedora-query-continuation-inputs/fedora-cache-controller.py`，固定 SHA-256 为 `22335a8d9e07d2f704b8dee09c39beaefb1d6ffeda1451ba61a47e2323872f52`。读取实际原回执的脚本 `check-amd-dnf-budget-provenance.py` 置于新私有变更目录，SHA-256 为 `70a0dbcebdd6f6c07bddfff9cc5d93e9ef2f13cbd185e85fa5496cbb2401b2c9`。该 profile 的 `check_preconditions` 不调用 `observe_device`，可以在原 ACL 下做只读 native/磁盘配置交叉验证。这一阶段不检查 live 设备权限和 ACL 看守，也不启动客体或写 cgroup 限额。

本地定向离线测试命令（从工作树执行）：

```bash
/usr/bin/python3 tools/offline_test_guard.py run \
  --out artifacts/v008-gpu-drm-20261115T183930+0800/amd-query-memory-guard-t1 \
  --seconds 120 -- /usr/bin/python3 -m unittest discover -s tests -p "test_hardware_guard.py" -v
```

远端实际 provenance 只读核验命令（下发并核对 SHA 之后执行）：

```bash
RUN=/home/agent/v008-gpu-drm-20261115T183930
/usr/bin/python3 "$RUN/source/tools/offline_test_guard.py" run \
  --out "$RUN/amd-query-memory-update/provenance-t1" \
  --seconds 120 -- /usr/bin/python3 -B \
  "$RUN/amd-query-memory-update/check-amd-dnf-budget-provenance.py" "$RUN/source"
```

主控已在 `amd-query-continuation-t1` 验收 87 项，退出码 0、无保护中止。远端只读核验和现场限额调整仍待执行。
