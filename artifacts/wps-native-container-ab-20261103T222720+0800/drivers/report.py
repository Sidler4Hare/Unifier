import json,subprocess,os,statistics,hashlib,time
from pathlib import Path
P=Path(__file__).resolve().parent;R=P.parents[2];E=Path((P/'evidence-path').read_text().strip());os.environ['GOMAXPROCS']='2'
assert (P/'finish.json').exists(),'cleanup has not finished'
def read(label):return json.loads((P/'cells'/label/'result.json').read_text())
sets=[('Writer',['CWNA'],['CWGB']),('Sheets',['CSNA'],['CSGB']),('Slides',['CPNA'],['CPGB']),('三组件同开',['CFNA','CFNB'],['CFGC','CFGD'])]
allcells=[];table=[];structured=[]
for name,ns,gs in sets:
 n=[read(l) for l in ns];g=[read(l) for l in gs];allcells+=n+g
 for c in n+g:
  assert c['status']=='MEASURED' and c['summary']['pss_errors']==0,c['label']
  assert c['summary']['app_processes_median']==(3 if c['case']=='family' else 1),c['label']
  if c['side']=='guest':assert not any(w['name']=='WPS Office' for w in c['windows_sampled']),c['label']
 nm=statistics.median(c['summary']['app_pss_median_mib'] for c in n);gm=statistics.median(c['summary']['app_pss_median_mib'] for c in g)
 nc=statistics.median(c['summary']['idle_cgroup_cpu_one_core_pct'] for c in n);gc=statistics.median(c['summary']['idle_cgroup_cpu_one_core_pct'] for c in g)
 structured.append({'case':name,'native_labels':ns,'guest_labels':gs,'native_pss_mib':nm,'guest_pss_mib':gm,'difference_mib':gm-nm,'difference_percent':(gm/nm-1)*100,'native_idle_cpu_one_core_percent':nc,'guest_idle_cpu_one_core_percent':gc})
 table.append(f'| {name} | {nm:.1f} | {gm:.1f} | {gm-nm:+.1f} ({(gm/nm-1)*100:+.1f}%) | {nc:.2f}% / {gc:.2f}% |')
finish=json.loads((P/'finish.json').read_text());inputs=json.loads((P/'comparison-inputs.json').read_text())
ids=subprocess.check_output(['podman','ps','-aq'],text=True).split();raw=json.loads(subprocess.check_output(['podman','inspect',*ids]))
inventory=[]
for c in raw:
 labels=c['Config'].get('Labels') or {};project=labels.get('multilinux.project','');kind='日常' if project.startswith('/home/omg/.local/share/multilinux/apps/') else '测试'
 inventory.append({'id':c['Id'],'name':c['Name'],'kind':kind,'project':project,'created':c['Created'],'status':c['State']['Status'],'running':c['State']['Running'],'memory_limit':c['HostConfig']['Memory'],'nano_cpus':c['HostConfig']['NanoCpus'],'pids_limit':c['HostConfig']['PidsLimit']})
(E/'container-inventory.json').write_text(json.dumps(inventory,ensure_ascii=False,indent=2))
metrics=[]
for c in allcells:
 for s in (P/'cells'/c['label']/'samples.jsonl').read_text().splitlines():metrics.append(json.loads(s))
events_ok=all(all(m['memory_events'].get(k,0)==0 for k in ['high','max','oom','oom_kill']) and m['pids_events'].get('max',0)==0 for m in metrics)
all_swap=max(c['summary']['app_swap_pss_median_mib'] for c in allcells)
trials=[json.loads(s) for s in (P/'guardian-samples.jsonl').read_text().splitlines()]
hostmin=min(x['MemAvailable'] for x in trials)/1024**2;psimax=max(float(x['psi']['full']['avg10']) for x in trials)
nativehash=json.loads((E/'native-fixtures-after.json').read_text());guesthash={s.split()[-1].rsplit('/',1)[-1]:s.split()[0] for s in (E/'guest-fixtures-after.sha256').read_text().splitlines()}
fixtures_ok=all(nativehash.get(k)==guesthash.get(k)==inputs['fixture_font_hashes'][k] for k in ['writer.docx','sheet.xlsx','slides.pptx'])
summary={'scope':'PILOT_LOCAL_CORE_EDITORS_CLOUD_DISABLED','cells':structured,'valid_cells':len(allcells),'snapshot_count':len(metrics),'events_clear':events_ok,'max_median_app_swap_pss_mib':all_swap,'host_min_available_mib':hostmin,'host_full_psi_max_avg10':psimax,'fixtures_unchanged':fixtures_ok,'cleanup':finish,'container_count':len(inventory),'running_containers':sum(c['running'] for c in inventory)}
(E/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
runtime_rows=[];helpers=[]
for name in ['final-container-idle.jsonl','final-unifier-wps-ab-prep.service.jsonl','final-unifier-wps-ab-display.service.jsonl']:
 if not (P/name).exists():continue
 m=json.loads((P/name).read_text().splitlines()[-1]);ps=sum(r['pss'] for r in m['rows'])/1024**2
 runtime_rows.append(f'- `{name}`：可读进程 PSS 合计 {ps:.2f} MiB，读取错误 {len(m["errors"])} 个。')
 for r in m['rows']:
  if Path(r['exe']).name in ['conmon','pasta','pasta.avx2','podman-init','sleep']:helpers.append(r)
helperps=sum(r['pss'] for r in {r['pid']:r for r in helpers}.values())/1024**2
invrows='\n'.join(f'| `{c["id"][:12]}` | {c["name"]} | {c["kind"]} | {c["status"]} |' for c in sorted(inventory,key=lambda c:(c['kind'],c['created'])))
report=f'''# Fedora 原生与项目容器内 WPS 占用对照

日期：2026-11-03，Asia/Shanghai。结论等级：**本地三组件资源小样本完成；完整云功能套件对照 PARTIAL**。

## 结果

在同版本、同核心二进制、相同文档/字体、独立组件模式和软件渲染下，**没有观察到容器使本地 WPS 三组件内存明显放大**。以下为全部应用核心进程 PSS 的中位数，单位 MiB；CPU 是静置期间占一个逻辑核的百分比。

| 场景 | Fedora 原生 PSS | Debian 容器 PSS | 容器减原生 | 静置 CPU 原生 / 容器 |
|---|---:|---:|---:|---:|
{chr(10).join(table)}

单组件每侧 1 轮，三组件同开每侧 2 轮，每轮稳定后每秒采样 16 次，共 {len(allcells)} 个比较单元、{len(metrics)} 个快照。单开按交替顺序运行；提示框补测后的总体顺序不满足正式随机配对要求。**这些数值只说明本轮量级，不能据此声称普遍节省内存、吞吐更快或已经排除泄漏。**

有效样本应用 SwapPss 中位数最大值 {all_swap:.2f} MiB；PSS 读取完整。预算/OOM/PID 事件均为零：{events_ok}；CPU 节流增量均为零。测试控制器、显示和网络辅助分别记录，未加到应用 PSS 内。

## 对照条件与边界

- 用户日常 Fedora 安装仍为 `wps-office-12.1.2.28080.AK.preread.sw-1.x86_64`。没有升级、覆盖或卸载。
- 主对照使用 [WPS 官方 Linux 下载页](https://www.wps.cn/product/wpslinuxlog) 的 29183 RPM，在私有目录解包；只在原生测试进程的挂载命名空间中映射到安装路径。执行的是 Fedora 本机用户态程序与库，没有创建 Fedora 容器。
- 容器是既有 Unifier 创建的 Debian 13 测试容器 `{finish.get('container_stopped') and '8399ccd38b13'}`，无宿主目录/桌面挂载；同版 DEB 解包到其私有目录，补入 `libxkbcommon-x11-0`、`libxcb-xkb1` 两个运行依赖。宿主未安装这些测试工具。
- `wps/et/wpp/wpsoffice/libkso.so` 共 5 项 SHA-256 在 RPM 与 DEB 中完全一致。尚未证明全部插件和系统动态库一致，因此这也包含 Fedora/Debian 用户态的差异，**不是纯命名空间成本实验**。
- 两侧使用相同哈希的 DOCX/XLSX/PPTX、Noto 字体和最小 Office.conf，1280×800×24 的同一独立 Xvfb，通过两条相同的本机回环 TCP→Unix 转发访问。显示/转发只是测试装置，未连接活动 KWin/Wayland/X0。
- 原生侧使用私有 TMP、IPC 和 `/dev/shm`，独立 HOME/XDG 配置，清除活动桌面会话环境；两侧均无可用 DRM 设备，使用软件渲染。原生软件在发行版内核上直接运行；容器共享同一宿主内核。
- **本表两侧均禁用了各自测试副本的云辅助可执行文件**，只测本地 Writer/Sheets/Slides。原始完整原生启动反复出现 `wpscloudsvr SIGSEGV`，而容器内该辅助进程及 CEF 可以存活。进程集合不同的原始总量不得当作容器开销。异常原因尚未定位，未把它归因于 Unifier、OOM 或 Fedora 本身。
- 单开截图已用于核对中文文档、表格数值和幻灯片；容器侧“设为默认应用”提示会使样本失效，相关旧样本已排除并补测。补测通过关闭提示框取消，不接受设置默认应用。
- 本轮是打开/稳定空闲资源对照，没有执行固定编辑吞吐、保存延迟、GPU/Wayland 帧率、长时压力或云账号功能验收。部分关闭操作会弹出确认框，之后由测试清理结束进程，不能冒充正常关闭/保存验收。输入文档哈希始终一致：{fixtures_ok}。

## 框架、缓存与测试装置

本轮停止应用后，可读的 init/sleep/conmon/pasta 进程 PSS 合计约 {helperps:.2f} MiB。这只是该测试运行时辅助进程的观测子集，**不代表整个项目全部服务的总开销**，也不包含图形服务器、控制器、FUSE/代理等未启用路径。

{chr(10).join(runtime_rows)}

容器 `memory.current` 包含解包和运行留下的文件页缓存；原生映射的共享文件也可能记账到之前加载它的 cgroup。因此不能用两个 `memory.current` 的差值直接称为应用内存差，更不能与 PSS 相加。逐点 anon/file/shmem、cgroup 事件、CPU、PSS/SwapPss 已保留。

## 保护记录

- 尝试 1：容器内直接解包阶段 MemAvailable 下降至约 1.85 GiB，2 GiB 宿主保留线触发，自动停止；没有启动 WPS。随后改成宿主受限单线程解压、限速输入容器，避免高并发解压缓冲/写入突发。
- 尝试 2：原生预检期间 memory PSI full avg10=3.26% 持续到停止阈值，保护再次停止；当时 MemAvailable 仍约 3.74 GiB，说明只看空闲量不足。未降低保留线或压力阈值。
- 两次停止回执中的 boot ID、KWin PID 均不变；均没有将停止样本算作通过。原生云辅助 SIGSEGV 与这些保护事件分开记录，不能混同为桌面崩溃或 OOM。
- 最后本地编辑模式一轮宿主可用内存最低约 {hostmin/1024:.2f} GiB，full PSI avg10 最大 {psimax:.2f}%，没有触发停止。
- 容器 CPU/RAM 配额保持 `0`（未设置），PID 上限保留原有 2048；CPU 无 quota。原生测试单元有 1400 MiB 紧急预算和 768 任务上限，实际未触发。保护是本机测试作业策略，**不据此给日常应用增加 CPU/RAM 硬限额**。
- 之前将“容器没硬上限”列为产品首要缺陷的说法过强。证据支持的是测试编排需要准入、串行、整体监测和自动停止；普通容器无限额不等于项目性能缺陷。

## 容器为什么多

当前总计 {len(inventory)} 个：2 个日常环境，8 个历史测试环境；本轮新增 **0**，复用了 auto-deps 的 Debian 测试容器。当前运行数 {sum(c['running'] for c in inventory)}。

| CID 前缀 | 名称 | 用途类别 | 收尾状态 |
|---|---|---|---|
{invrows}

测试容器的来源分别为：10 月 31 日扩大测试 Debian/Arch 各一个；自动依赖两轮各保留 Debian/Arch，共四个；11 月 1 日打断恢复一个；11 月 3 日隔离 GUI 快照一个。今天此前新增保留的是最后这个 GUI 测试容器。保留它们用于故障回放和数据恢复，不意味着产品需要十个环境同时运行。

停止的容器没有在执行其应用进程；镜像/可写层仍占磁盘，页缓存也可能暂留并可回收。本轮没有删除容器和历史数据。后续按规范登记复用/保留期限，再针对精确 CID 备份归档，避免每轮新增一批副本。

## 收尾与交付

- 测试容器已停止：{finish['container_stopped']}；原有 HostConfig 不变：{finish['host_config_unchanged']}；挂载不变：{finish['mounts_unchanged']}。
- 私有原生/容器测试副本的云可执行文件已恢复：{finish.get('native_cloud_restored')} / {finish.get('guest_cloud_restored')}。宿主日常 RPM 保持原版本。
- 早期原生预检在共享 `/dev/shm` 中出现过同时间段的少量 `sem.*` 文件；后续改为每单元独立 tmpfs，未按名称模式删除无法确证归属的宿主 IPC 文件。保留范围待单独审查，不能称为零宿主临时写入。
- 未提交、部署或推送产品代码；测试输入、缓存包和私有解包目录位于本轮 `.lab/agents` 下，公开证据不含 Xauthority 内容、账号配置和完整容器 inspect。
- 测试规范：`docs/本机测试规范.md`。证据：`summary.json`、`comparison-inputs.json`、`core-firstpass.json`、`core-batch.json`、`cells/`、`attempt1/`、`attempt2/`、`drivers/`、`finish.json`。

完整云功能套件的严格对照仍未完成；本轮已完成的是关闭云辅助服务后的本地三组件占用对照。不能用本表否定此前真实发生的宿主内存压力，也不能将此前桌面事故全部归结为容器开销。
'''
(E/'report.md').write_text(report)
print(json.dumps(summary,ensure_ascii=False,indent=2))
