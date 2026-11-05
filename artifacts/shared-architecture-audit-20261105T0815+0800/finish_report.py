"""T0 report assembly from completed receipts; no tests or product actions."""
import collections,hashlib,json,os,re,subprocess
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
ROOT=Path.cwd();OUT=Path(__file__).resolve().parent

def save(name,x): (OUT/name).write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n')
def cmd(args):
 p=subprocess.run(args,env={**os.environ,'GOMAXPROCS':'1','GIT_OPTIONAL_LOCKS':'0'},capture_output=True,text=True,timeout=10)
 return p
before=json.loads((OUT/'before.json').read_text())
changed=[n for n,h in before['files'].items() if not Path(n).is_file() or hashlib.sha256(Path(n).read_bytes()).hexdigest()!=h]
kwin=[]
for p in Path('/proc').iterdir():
 if not p.name.isdecimal():continue
 try:
  if p.stat().st_uid==os.getuid() and (p/'comm').read_text().strip() in ('kwin_wayland','kwin_x11'):
   kwin.append({'pid':int(p.name),'starttime':(p/'stat').read_text().rsplit(')',1)[1].split()[19]})
 except (FileNotFoundError,PermissionError,ProcessLookupError):pass
containers=cmd(['podman','ps','-a','--no-trunc','--format','{{.ID}} {{.Names}} {{.State}}']);assert containers.returncode==0
index=hashlib.sha256(subprocess.check_output(['git','ls-files','--stage','-z'])).hexdigest()
stage_units={'batch1':'batch1','batch2':'batch2','native-offline':'native','counterexamples':'probes','background-retry':'background','qwen-review':'review','completions':'completions'}
receipts={n:json.loads((OUT/n/'receipt.json').read_text()) for n in stage_units}
units=[]
for suffix in stage_units.values():
 for role in ('guard','work'):
  name=f'unifier-audit-20261105-0815-{suffix}-{role}.service'
  p=cmd(['systemctl','--user','show',name,'-p','ActiveState','-p','MainPID','-p','ControlGroup','-p','InvocationID'])
  fields=dict(line.split('=',1) for line in p.stdout.splitlines() if '=' in line)
  units.append({'unit':name,**fields})
save('units-after.json',units)
pool=json.loads((OUT/'pool-readiness.json').read_text())
preservation={'checked_files':len(before['files']),'unexpected_changes':changed,'index_entries_equal':index==before['index_sha256'],
 'container_inventory_equal':containers.stdout==before['containers'],'container_count_before':len(before['containers'].splitlines()),'container_count_after':len(containers.stdout.splitlines()),
 'all_containers_exited':all(s.endswith(' exited') for s in containers.stdout.splitlines()),
 'pool_records_unchanged':all(hashlib.sha256(Path(p['record']).read_bytes()).hexdigest()==p['record_sha256'] for p in pool),
 'boot_identity_equal':before['boot_id']==Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'kwin_identity_equal':before['kwin']==kwin,
 'all_owned_units_exited':all(u.get('MainPID')=='0' and u.get('ActiveState') in ('inactive','failed') for u in units),
 'all_started_worker_cgroups_removed':all(r.get('cgroup_removed',True) for r in receipts.values()),
 'installed_entry_path_equal':before['installed_entry']==str((Path.home()/'.local/bin/unifier').resolve())}
save('preservation.json',preservation)
save('after.json',{'at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'head':cmd(['git','rev-parse','HEAD']).stdout.strip(),'index_sha256':index,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'kwin':kwin,'containers':containers.stdout,'git_status':cmd(['git','status','--short']).stdout})
(OUT/'host-after.txt').write_text(Path('/proc/meminfo').read_text()+Path('/proc/pressure/memory').read_text())
(OUT/'existing-slice-after.txt').write_text(cmd(['systemctl','--user','show','unifier-tests.slice','-p','TasksCurrent','-p','ControlGroup','-p','MemoryCurrent']).stdout)
all_samples=[]
for n in stage_units:
 for f in ('admission.jsonl','samples.jsonl'):
  for line in (OUT/n/f).read_text().splitlines() if (OUT/n/f).exists() else []:
   x=json.loads(line);all_samples.append(x.get('host',x))
resources={'reserve_bytes':1024**3,'min_mem_available_bytes':min(x['available'] for x in all_samples),
 'max_full_avg10':max(x['psi']['full']['avg10'] for x in all_samples),'max_some_avg10':max(x['psi']['some']['avg10'] for x in all_samples),
 'stages':{n:{'reason':r['reason'],'command_exit':r.get('command_result',{}).get('exit_code'),
  'peak_memory':r.get('worker_final_cgroup',{}).get('memory_peak'),'peak_tasks':r.get('worker_final_cgroup',{}).get('tasks_peak'),
  'memory_events':r.get('worker_final_cgroup',{}).get('memory_events'),'pids_events':r.get('worker_final_cgroup',{}).get('pids_events'),
  'cpu_stat':r.get('worker_final_cgroup',{}).get('cpu_stat'),'cgroup_removed':r.get('cgroup_removed')} for n,r in receipts.items()}}
save('resources.json',resources)
base=json.loads((OUT/'offline-case-outcomes.json').read_text())
assert base['unique']==734 and base['states']=={'PASS':727,'FAIL':1,'SKIPPED':6}
assert receipts['completions']['command_result']['exit_code']==0
assert receipts['background-retry']['command_result']['exit_code']==0
findings=[
 {'id':'F01','priority':'P1','kind':'functional regression and capability-contract gap','title':'Adding another distribution blocks normal package operations',
  'evidence':['batch2/command.log','counterexamples/cross-environment-admission.json'],'source':'src/unifier/service.py:1504; src/unifier/service.py:1839',
  'scope':'Existing normal CLI regression plus fake database counterexample and confirmed missing real runtime capability; no live Podman execution',
  'recommendation':'Define a real read-only observation capability and implement/verify the runtime adapter, or redesign the isolation proof around an explicit target authority. Preserve refusal until sound proof is available; do not hide the gap by adding fake-only support.'},
 {'id':'F02','priority':'P1','kind':'architecture correctness under authorized concurrency','title':'Authorized independent transactions become mutual isolation failures',
  'evidence':['counterexamples/concurrent-peer-false-positive.json','completions/same-environment-writer-lock.json'],'source':'src/unifier/service.py:159; src/unifier/service.py:1410',
  'scope':'Deterministic two-barrier fake-runtime simulation; no real concurrent containers. Same-environment writer serialization passes.',
  'recommendation':'Distinguish authorized operation generations/IDs from unexplained mutation. Define a consistency window for cross-environment observations and lock ordering. Do not call every peer change an isolation breach.'},
 {'id':'F03','priority':'P1 conditional','kind':'execution ownership gap','title':'App lease follows launcher completion rather than daemonized guest lifetime',
  'evidence':['counterexamples/background-app-lease.json','background-retry/command.log'],'source':'src/unifier/on_demand.py:373; src/unifier/runtime.py:716; src/unifier/project_sessions.py:785',
  'scope':'Host-owned background child simulates a guest app; environment runtime is fake. Specific real WPS/GUI impact UNKNOWN.',
  'recommendation':'Share controlled guest PID/start/token/probe/cancel ownership between APP, sessions and broker requests. Define foreground, background and single-instance activation lifetimes explicitly; do not stop a whole shared environment merely because a launcher exits.'},
 {'id':'F04','priority':'P2','kind':'capability and acceptance gap','title':'Bundled SONAME candidates do not prove runtime loader success',
  'evidence':['counterexamples/bundled-vs-loader.json'],'source':'src/unifier/dependencies.py:177',
  'scope':'Real tiny host-built ELF and dynamic-loader failure (127), combined with a fake package database. No guest library copying or real guest install. runtime_loading is already explicitly unverified.',
  'recommendation':'Keep static dependency discovery, actual loader resolution and workflow acceptance distinct. Report bundled candidates as unverified until RPATH/RUNPATH/ORIGIN and ABI or an actual bounded loader oracle establish usability.'}
]
save('findings.json',findings)
plan=json.loads(Path('artifacts/shared-container-dependency-plan-20261104/plan.json').read_text())
cases=[]
for c in plan['cases']:
 x={'id':c['id'],'title':c['title'],'status':'BLOCKED','offline_evidence':'Existing offline contracts exercised where applicable','live_status':'NOT_RUN','reason':'PRE02/PRE03/PRE04/PRE05 real execution prerequisites not fulfilled'}
 if c['id']=='PRE01':x.update(status='PASS',offline_evidence='Read-only full CID/UUID/generation/original root/labels/image/mounts/limits/state frozen in pool-readiness and pool-inspect',reason='Identity listing only; this does not admit live resource/fixture readiness')
 elif c['id']=='PRE02':x.update(offline_evidence='No-wake rejection contracts pass; actual multi-target CLI functionality fails (F01)',reason='Actual read-only peer inventory adapter missing; selected Debian root has no real same-root peer to validate that branch')
 elif c['id']=='PRE03':x.update(status='PARTIAL',offline_evidence='Independent T1 guardian, hard budget, heartbeat/deadline, real proc/cgroup and exit evidence',reason='T2 aggregate container monitoring, independent container cleanup on monitor/controller failure and fault-injection coverage not completed')
 elif c['id']=='PRE04':x.update(status='PARTIAL',offline_evidence='Formal fake CLI and native private ELF fixture; native C synthetic checks',reason='Missing real guest package/provider/version/GUI fixtures with current natural missing dependency proof')
 elif c['id']=='PRE05':x.update(status='PARTIAL',offline_evidence='213 source/test/tool/doc hashes, index/boot/KWin/container baseline and original pool records',reason='Full live fixture/account/data backup admission not performed; no live writes attempted')
 elif c['id']=='SH08':x.update(status='PARTIAL',reason='Identity/failed launch/unknown state offline branches pass; live recovery NOT_RUN')
 elif c['id']=='SH09':x.update(status='PARTIAL',offline_evidence='Two fake requests to same environment: max one native writer, both complete; cross-env false positive reproduced (F02)',reason='Actual native database lock competition and live coexistence NOT_RUN')
 elif c['id']=='DEP09':x.update(status='PASS',live_status='not required (offline case)',offline_evidence='Zero/multiple candidates and partial failed provider responses refuse without guessing',reason='No real repository claim')
 elif c['id']=='DEP10':x.update(status='PASS',live_status='not required (offline case)',offline_evidence='Index refresh exit73, install failure, ineffective install, recursive convergence bound and recovery contracts',reason='Bounded failure contracts verified offline; no live interrupted package transaction')
 elif c['id']=='DEP12':x.update(status='PARTIAL',offline_evidence='ABI/parser rejection contracts and real private ELF search-path counterexample (F04)',reason='Full symbol-version and vendor configuration classification/real guest oracle NOT_RUN')
 elif c['id']=='DEP13':x.update(status='PARTIAL',offline_evidence='Interrupted journal/database/replaced CID/recovery regressions pass',reason='No live fault injection or package recovery applied to preserved instances')
 elif c['id'].startswith('AR'):x.update(reason='Existing Arch has Memory=0, NanoCpus=0, TasksMax2048, cgroup parent user.slice; no compliant aggregate resource admission. No automatic quota update, new container or partial upgrade')
 elif c['id']=='END01':x.update(status='PASS',live_status='not applicable (none started)',offline_evidence='All exact job units/PIDs/cgroups exited; containers10->10 all exited; index and213 existing files preserved',reason='No daily-file whole-tree hash or continuous external container event attribution claimed')
 cases.append(x)
assert len(cases)==32
save('case-results.json',{'plan':'artifacts/shared-container-dependency-plan-20261104/plan.json','cases':cases})
result={'status':'PARTIAL_WITH_CONFIRMED_FAILURE','finished_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'source_head':before['head'],
 'existing_offline_suite':{'unique':734,'passed':727,'failed':1,'skipped':6,'fixture_errors_initial':3,'fixture_replays_passed':3},
 'extra_contracts':{'unique':3,'passed':3},'combined_correctness_tests':{'unique':737,'passed':730,'failed':1,'skipped':6},
 'architecture_diagnostics':{'unique':4,'observations_confirmed':4,'note':'Diagnostic green means observed limitation reproduced, not fixed','initial_evidence_serialization_error':1,'single_case_replay_passed':1},
 'qwen_review':'NOT_COMPLETED: correct local-server/swift-1.5-iq3_s/max route audited; 90-second request timeout, no final or tools, no acceptance claim',
 'source_changes':[],'containers_created':0,'containers_started':0,'live_cases_executed':0,'deployed':False,'committed':False,'pushed':False,
 'preservation':preservation,'resources':resources,'findings':[f['id'] for f in findings],
 'not_complete':'Real shared-container CLI/package/GUI matrix is blocked and not accepted; all32 cases have explicit current dispositions'}
save('result.json',result)
minmem=resources['min_mem_available_bytes']/1024**3
report=f'''# 共享容器、依赖与架构审查结果（2026-11-05）

**离线可执行范围已覆盖，结果仍不合格；真实容器/GUI专项尚未完成。** 当前工作树存在1项正常CLI回归，并复现了隔离归因、后台应用租约和加载器验收的不足。共享宿主内核、发行版原生用户态、完整实例身份与原生包恢复这条基础路线可以保留；需要调整的是控制面的能力契约、执行所有权与隔离观察方式。

本轮保持产品和既有测试源码不变，完整保留11月4日工作树修复、脏文件和索引。没有部署、提交、推送、新建或启动容器。工作树HEAD：`{before['head']}`；日常入口仍为原安装版，不能把当前工作树缺陷直接当成已部署行为。

## 结果与统计口径

| 范围 | 去重项数 | 通过 | 失败 | 跳过 |
|---|---:|---:|---:|---:|
| 当前既有离线套件 | 734 | 727 | 1 | 6 |
| 补充合同：四类操作前拒绝、索引失败、同环境写锁 | 3 | 3 | 0 | 0 |
| 正确性检查合计 | **737** | **730** | **1** | **6** |

另有4项架构诊断，四项均确认了预设观察。这些诊断“通过”表示反例复现，**不计为问题已修复**。6项跳过涉及真实Podman安装/生命周期、GUI和真实GPU/设备条件；不算通过。原生C合成memfd/socketpair、参数与不存在设备的错误路径实际编译运行，未连接宿主屏幕/音频/摄像头/默认PipeWire。

原始批次：batch1=157项（154通过、3报错）；batch2=563项（562通过、1失败，包含前3项重放）；native-offline=17项（11通过、6跳过）。按完整测试ID去重后得到734项，详见 `offline-case-outcomes.json`。前3项错误是fake ProxyPool把相对bus重组为过长绝对路径；产品已使用相对bind地址。采用新的短私有临时目录后3项重放通过，原始错误与退出码保留，未把它们算成产品错误。诊断探针另有一次CreateSpec证据JSON序列化错误，精确子进程正常收尾；仅修正证据收集，单项重放通过。

## 问题与不足

### F01 / P1：多发行版包管理的实际功能回归

正常回归用例 `test_cli_shortcuts.RepairContracts.test_same_alias_is_namespaced_per_target_and_run_picks_the_right_instance` 失败。单发行版install成功；加入第二发行版后，同一目标下一次install在 `isolation-preflight` 被拒绝，native为空，未唤醒同伴。真实PodmanRuntime确实没有 `read_inventory_readonly`。

上次改动解决了“观察时唤醒同伴”的副作用，**尚未完成可用的真实多环境修复**。同一管理根中的包事务依赖所有无关环境都能被独立读取；缺一个适配能力就阻断目标。现有fake PackageBoxes提供该能力，而真实runtime没有，导致局部绿灯掩盖了正常CLI的可用性回归。

源码：[前置拒绝](/home/omg/文档/多兼容linux机/src/unifier/service.py:1504)、[只读能力缺口](/home/omg/文档/多兼容linux机/src/unifier/service.py:1839)。证据：`batch2/command.log`、`counterexamples/cross-environment-admission.json`。

优先完成明确的Runtime能力契约与真实只读数据库适配，或重新定义目标隔离证明的范围。隔离校验必须保留真实证据；不能以删除同伴记录、关闭检查或补一个fake方法使其表面通过。

### F02 / P1：把授权的并发变更误判为隔离污染

两个不同环境各自持有包锁，均获得当前的独立只读快照。双barrier保证两者都在before快照后完成各自包写入、再做after比较。两个包实际均已安装，但A报告“B inventory changed”，B报告“A inventory changed”，两个事务均为failed/verification。

同环境的两个请求已证明串行，原生写入最大并发1、均completed。问题位于跨环境观察模型：它把任何同伴变化当成越界，不区分另一个已授权事务。这个反例使用fake环境，不是真实并行容器结果。

源码：[前后比较](/home/omg/文档/多兼容linux机/src/unifier/service.py:159)、[每环境锁](/home/omg/文档/多兼容linux机/src/unifier/service.py:1410)。证据：`counterexamples/concurrent-peer-false-positive.json`、`completions/same-environment-writer-lock.json`。

需要为授权包操作建立operation ID/代际与一致性窗口，明确锁的顺序。若必须顺序化跨环境审计，应明确排队，而不是在合法写入后给出隔离失败；也不能把“变化来自另一个请求”直接当安全通过。

### F03 / P1（条件性）：APP租约未统一到实际客体执行所有权

managed_open在启动器返回后finally释放lease，runtime.exec只等待podman exec返回。包事务/项目会话已有客体PID/start/token与probe/cancel路径，APP未统一使用。

host自有后台子进程模拟客体应用后台化：启动器返回0后，child仍活，lease已归零，fake环境已经stopped。证明控制模型缺口；**尚未证明某一真实WPS/GUI应用受到影响**，更不能把宿主模拟说成真实容器生命周期验收。

源码：[lease释放](/home/omg/文档/多兼容linux机/src/unifier/on_demand.py:373)、[普通exec](/home/omg/文档/多兼容linux机/src/unifier/runtime.py:716)、[已有客体身份](/home/omg/文档/多兼容linux机/src/unifier/project_sessions.py:785)。证据：`counterexamples/background-app-lease.json`、`background-retry/command.log`。

建议APP、项目会话、Agent请求复用受控执行所有权，分别定义前台命令、后台应用和单实例激活的完成条件。无法证明实际执行结束时保留未知状态和可恢复租约，不按进程名清理、不因一个启动器退出停止整个共享环境。

### F04 / P2：静态补库与可加载性仍有明确边界

真实微型ELF：main链接一个库但不带RPATH，库在另一个私有目录。fake包数据库扫描能看到两者，静态修复报告completed/no-missing-elf-dependencies；实际host动态加载器退出127、找不到该库。源码把所有对象的SONAME/basename视为bundled候选，没有按每个对象解析RPATH/RUNPATH/ORIGIN、符号版本等。

当前字段已经明确 `runtime_loading=unverified`，所以这是一项能力与验收缺口，不能说实现谎报“已验证实际加载”。它也不是一次真实guest安装结果。真实二进制哈希、源码、readelf输出和实际stderr均保留。

源码：[bundled候选计算](/home/omg/文档/多兼容linux机/src/unifier/dependencies.py:177)。证据：`counterexamples/bundled-vs-loader.json`。建议在产品展示和判定中区分安装、静态候选、实际加载、实际工作流；必要时加入有预算、普通用户的加载oracle，不能靠全局LD_LIBRARY_PATH或伪造库名凑成功。

## 架构取舍

保留完整CID/UUID/generation的执行前核验、同target唯一性、原生包数据库锁、持久事务日志与未知状态拒绝、共享租约这些基础机制。本轮大量正向与拒绝合同支持它们的价值；不代表完整实机验收。

建议按实际故障边界划分职责，而不以文件行数为重构理由：

```mermaid
flowchart LR
    E[CLI / APP / 项目会话 / Agent] --> X[统一执行所有权与恢复]
    X --> I[实例身份与共享租约]
    X --> R[Runtime能力适配]
    P[包事务与原生数据库锁] --> R
    P --> O[隔离观察：能力 / 操作代际 / 一致性窗口]
    X --> B[测试或产品各自的资源准入策略]
```

这是建议职责边界，尚未实现。先修F01真实能力闭环并让现有失败用例通过，再验证F02授权并发，随后统一APP执行所有权与F04验收层次。实际产品资源策略和测试预算分别设计；容器未设置某个默认硬配额，本身不能直接判为产品架构或性能缺陷。

## 32项真实专项的完成边界

逐条处置见 `case-results.json`。PRE01只读身份核对、DEP09/DEP10离线负向合同与本轮END01收尾完成；PRE02实际能力和PRE03实机保护仍阻断。共享CLI/依赖真实安装/Writer保存重开/Arch专项未执行，不把737项离线检查代替这些结果。

- Debian：原CID、UUID/代际、所有权标签、镜像、私有挂载、768MiB/192任务等读取核对；原根只有一个环境，没有真实同伴可以独立实测PRE02分支。T2聚合容器cgroup覆盖、保护器失联时的独立客体收尾与全部故障注入尚未完成。T1装置只会收尾自己的worker，不能据此放行另一个scope里的真实容器。
- Arch：原CID/根匹配，但Memory=0、NanoCpus=0、2048任务、父组user.slice，尚无合规汇总预算；未自动改配额、重建或用另一旧容器绕过。
- 实机缺库正向样本仍需自然缺失条件、可信唯一provider、版本/哈希、正式入口和最终真实运行证据；未通过卸载公共库制造缺失或手工补库回填。

## 资源、模型委派与收尾

沿用用户授权的 **1GiB** 保留线；负载512MiB/96任务/1核/无swap，独立保护64MiB/16任务；每250ms采样、30秒低压力准入、总期限和实际整树cgroup核验。所有留存宿主采样MemAvailable最低 **{minmem:.3f}GiB**，各阶段memory.peak/pids.peak、OOM/max和CPU节流在 `resources.json`，不据此发布性能SLO。

本地Qwen仅收到受限摘录进行架构短评，当前路由脱敏核验为local-server/swift-1.5-iq3_s/max；请求90秒未形成final，按期限结束，未调用工具。**没有完成独立模型复核，不引用其结论**，本报告基于主控源码核查与已留存反例。未回退其他模型或扩大期限。

收尾：容器 **10→10，全部exited，新增/启动0**；全部本轮单元MainPID=0、工作cgroup移除，原测试slice TasksCurrent=0；没有显示、监听、真实包事务或客体执行。213个既有源码/测试/工具/文档摘要、Git索引条目、boot/KWin身份及原池登记文件均保全，见 `preservation.json`。

没有修改宿主配置、区域设置、日常部署或活动桌面。私有临时目录按各spec记录并保留必要材料；模型路由覆盖和原始日志在700权限的.lab目录，未公开凭据。未做日常数据全树字节摘要或连续外部容器事件流，不将前后清单一致外推为所有外部事件均已排除。
'''
(OUT/'report.md').write_text(report)
print(json.dumps({'correctness':result['combined_correctness_tests'],'preservation':preservation,'min_available_gib':minmem,'report':str(OUT/'report.md')},ensure_ascii=False))
