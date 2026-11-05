"""T0: summarize existing receipts and preservation; never run product/tests."""
import hashlib,json,os,re,subprocess
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
root=Path.cwd(); out=Path(__file__).resolve().parent

def save(name,x): (out/name).write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n')
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
kwin=[]
for p in Path('/proc').iterdir():
 if not p.name.isdigit(): continue
 try:
  if p.stat().st_uid==os.getuid() and (p/'comm').read_text().strip() in ('kwin_wayland','kwin_x11'):
   kwin.append({'pid':int(p.name),'starttime':(p/'stat').read_text().rsplit(')',1)[1].split()[19]})
 except (FileNotFoundError,PermissionError,ProcessLookupError): pass
save('kwin-after.json',kwin)
untouched=json.loads((out/'unrelated-files-before.json').read_text())
changed=[n for n,h in untouched.items() if not (root/n).exists() or sha(root/n)!=h]
receipts={n:json.loads((out/n/'receipt.json').read_text()) for n in ('red','red-v2','red-v3','qwen','qwen-v2','validation','boundaries')}
all_samples=[]
for name in receipts:
 for filename in ('admission.jsonl','samples.jsonl'):
  p=out/name/filename
  if not p.exists(): continue
  for line in p.read_text().splitlines():
   row=json.loads(line); all_samples.append(row.get('host',row))
units=['unifier-pre02-20261104-1510-'+x+'-'+s+'.service' for x in ('red','red2','red3','qwen','qwen2','validation','boundaries') for s in ('guard','work')]
unit_state=[]
for unit in units:
 p=subprocess.run(['systemctl','--user','show',unit,'-p','ActiveState','-p','MainPID','-p','ControlGroup','-p','InvocationID'],capture_output=True,text=True,timeout=5)
 fields=dict(line.split('=',1) for line in p.stdout.splitlines() if '=' in line)
 unit_state.append({'unit':unit,**fields})
save('unit-identities-after.json',unit_state)
pool=json.loads((out/'pool-records-readonly.json').read_text())
pool_unchanged=all(sha(Path(r['record_path']))==r['record_sha256'] for r in pool)
preservation={'untouched_files_checked':len(untouched),'unexpected_source_doc_changes':changed,
 'index_entries_equal':(out/'index-before.sha256').read_bytes()==(out/'index-after.sha256').read_bytes(),
 'containers_equal':(out/'containers-before.txt').read_bytes()==(out/'containers-after.txt').read_bytes(),
 'containers_before':len((out/'containers-before.txt').read_text().splitlines()),
 'containers_after':len((out/'containers-after.txt').read_text().splitlines()),
 'all_containers_exited':all(line.endswith(' exited') for line in (out/'containers-after.txt').read_text().splitlines()),
 'pool_records_unchanged':pool_unchanged,
 'boot_equal':(out/'boot-before.txt').read_bytes()==(out/'boot-after.txt').read_bytes(),
 'kwin_identity_equal':json.loads((out/'kwin-before.json').read_text())==kwin,
 'all_owned_units_have_no_live_pid':all(x.get('MainPID','0')=='0' and x.get('ActiveState') not in ('active','activating','deactivating') for x in unit_state),
 'owned_cgroups_removed':all(x.get('cgroup_removed',True) for x in receipts.values())}
save('preservation.json',preservation)
with (out/'source.patch').open('wb') as f:
 subprocess.run(['git','diff','--','src/unifier/service.py','tests/test_packages.py'],stdout=f,check=True)
 subprocess.run(['git','diff','--no-index','--','/dev/null','tests/test_package_observation.py'],stdout=f)
save('source-final.json',{n:sha(root/n) for n in ('src/unifier/service.py','tests/test_packages.py','tests/test_package_observation.py')})
log=(out/'validation/command.log').read_text()
assert re.search(r'Ran 64 tests',log) and log.rstrip().endswith('OK')
assert receipts['validation']['command_result']['exit_code']==0 and receipts['validation']['reason'] is None
plan=json.loads((root/'artifacts/shared-container-dependency-plan-20261104/plan.json').read_text())
cases=[{'id':x['id'],'title':x['title'],'status':'NOT_RUN','execution_mode':x['execution_mode']} for x in plan['cases']]
updates={
 'PRE01':{'status':'BLOCKED','detail':'Candidate original CID/generation records match; full live mount/device/IPC/owner admission not completed; no CID admitted'},
 'PRE02':{'status':'BLOCKED','offline_status':'PASS','detail':'Old code reproduced FAIL; corrected observation contract passes 8 new tests. Real Podman independent read-only inventory adapter unavailable; real peer branch NOT_RUN'},
 'PRE03':{'status':'BLOCKED','offline_status':'PARTIAL','detail':'T1 whole-tree cgroup limits and separate guardian verified; T2 container coverage/fault injection not completed; final additional stage refused by PSI admission'},
 'PRE04':{'status':'NOT_RUN','detail':'No live install/loader/GUI fixture preparation'},
 'PRE05':{'status':'PARTIAL','detail':'Source backups/index/pool records/container baseline preserved; no account or full live fixture backup/admission'},
 'DEP09':{'status':'PASS','execution_mode':'offline','detail':'Controlled zero/multiple/partial-failed provider responses; no real repository claim'},
 'DEP10':{'status':'PARTIAL','execution_mode':'offline','detail':'Installation nonzero, ineffective repair, failed query and recursive bound covered; full index-failure/recovery matrix not completed'},
 'DEP12':{'status':'PARTIAL','execution_mode':'offline','detail':'Wrong ABI/incomplete framing/untrusted providers covered; actual loader/symbol-version/vendor-config fixture NOT_RUN'},
 'END01':{'status':'PASS','execution_mode':'readonly_and_owned_cleanup','detail':'No live containers/apps started; all owned process groups exited; exact container/index/210 unrelated-file preservation checked; daily data full-tree hash not claimed'}
}
for x in cases: x.update(updates.get(x['id'],{}))
save('case-results.json',{'source_plan':str(root/'artifacts/shared-container-dependency-plan-20261104/plan.json'),'cases':cases})
resources={'reserve_bytes':1024**3,'override':'Explicit user authorization in this chat; only this run',
 'min_available_bytes':min(x['available'] for x in all_samples),
 'max_full_avg10':max(x['psi']['full']['avg10'] for x in all_samples),
 'max_some_avg10':max(x['psi']['some']['avg10'] for x in all_samples),
 'validation_min_available_bytes':receipts['validation']['min_available_bytes'],
 'validation_observed_peak_bytes':receipts['validation']['peak_memory_bytes'],
 'validation_observed_peak_tasks':receipts['validation']['peak_tasks'],
 'validation_final_events':receipts['validation']['worker_final_cgroup'],
 'memory_peak_counter':'UNKNOWN; sampled memory.current peak only, no memory.peak captured',
 'final_admission_refusal':receipts['boundaries']['reason']}
result={'status':'PARTIAL','finished_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
 'source_head':(out/'head.txt').read_text().strip(),
 'execution':'T1 offline only','red_tests':{'ran':1,'failures':1,'exit_code':1},
 'post_fix_tests':{'ran':64,'passed':64,'failures':0,'errors':0,'skipped':0,'exit_code':0},
 'additional_boundary_tests':'NOT_RUN: no qualified 30s PSI window within 60s',
 'qwen':'First attempt missing credential before model; second bounded attempt timed out without edits; Codex implemented/reviewed/tested fix',
 'resources':resources,'preservation':preservation,
 'live_tests_run':0,'containers_created':0,'containers_started':0,'host_settings_modified':False,
 'deployed':False,'committed':False,'pushed':False,'workflow_acceptance':'NOT_RUN',
 'product_limit':'Without a genuine read_inventory_readonly runtime capability, package transactions in a root with peer registrations now refuse before native writes. Podman has no implementation in this change.',
 'remaining':'Real peer inspection, complete T2 protection/fixed-pool admission, CLI/package/GUI shared-container cases, Arch quota admission, and final extra boundary checks'}
save('result.json',result)
min_gib=resources['min_available_bytes']/1024**3
report=f'''# 共享容器与依赖专项测试：第一批离线结果

日期：2026-11-04（Asia/Shanghai）。结论：**PARTIAL；修复后64项离线测试全部通过，真实容器/GUI未执行**。

本轮按用户明确授权，将本轮 MemAvailable 保留线从2GiB改为 **1GiB**，未改全项目默认规范或日常产品策略。保留整树硬预算、独立监测、PSI、30秒准入、超时和新增容器0约束。

## 实际结果

- PRE02 修复前：`red-v3/command.log`，**Ran 1，失败1，退出1**。一次正式fake包事务为核验同伴清单，对停止的同伴执行了两轮 start→exec→stop。这是有效产品契约失败，不是保护失败。
- 修复后：`validation/command.log`，**Ran 64，全部通过，skipped=0，退出0**。覆盖新增8项观察合同，以及原有包事务、依赖、恢复、旧保护器离线合同和共享租约收尾。
- 验收命令：`python3 -m unittest test_package_observation test_packages test_dependencies test_package_recovery test_guarded_test test_on_demand -v`；实际环境关闭真实集成、私有HOME/XDG/TMP，工作树源入口。
- 追加3项边界检查：**NOT_RUN/BLOCKED**。60秒内未获得完整连续30秒低压力窗口，独立保护器未启动负载；原因见 `boundaries/abort.json`。不降低PSI门槛继续。新增参数化候选用例未进入最终测试文件；原候选保留在 `boundaries/proposed-test-version.py`，不能计入64项通过。

## 工作树改动与边界

`src/unifier/service.py` 不再为了观察同伴调用 start/stop/exec。只有运行时明确提供独立、最新的 `read_inventory_readonly(record)` 才读取；缺能力、读取错误、非文本或与登记不一致时，在目标启动和原生包写入前，以 isolation-preflight 明确拒绝，保留事务与隔离证据。事后独立快照发生变化仍使验证失败。

`tests/test_packages.py` 为可控fake增加读取其当前内部包集合的只读能力；`tests/test_package_observation.py` 新增8项有区分力的回归，包含停止/运行同伴、缺能力、读失败、非文本、外部包变化和外来身份。

**真实 PodmanRuntime 的只读同伴清单适配器仍未实现。** 管理根里有其他环境登记时，相关真实包事务现在会安全拒绝；没有同伴的原有正常路径保留。fake正向通过不证明真实只读数据库已实现。本轮不是已部署修复，未提交、发布或更新日常入口。

## 资源与保护

T1负载：MemoryMax=512MiB、MemoryHigh=384MiB、swap=0、96任务、1CPU；独立保护器64MiB/16任务。实际memory.max/swap/pids/cpu、PID/start和cgroup均留在各阶段receipt。保护器位于负载组外，每250ms采样；BindsTo处理保护器退出，工作进程心跳处理失联，systemd总期限兜底。T2容器保护和全部故障注入合同尚未验收，不能由T1结果外推。

本轮所有留存宿主采样 MemAvailable 最低 **{min_gib:.3f}GiB**。64项验证阶段采样峰值约 **{resources['validation_observed_peak_bytes']/1024**2:.1f}MiB**、{resources['validation_observed_peak_tasks']}个任务；最终memory.max/OOM和pids.max事件均为0，CPU节流为0。未取得memory.peak计数器，以上只称采样峰值，不报告性能SLO。

保留全部准备失败：red首轮日志创建竞态、red-v2 PSI启动拒绝、本地模型首轮凭据缺失和第二轮255秒期限未产出编辑。Qwen路由经脱敏dump核验为local-server/swift-1.5-iq3_s/max，没有远端收费回退；模型日志/路由覆盖在受限.lab目录，不含公开凭据。模型未完成后由主控接手，不把其自报当验收。

## 收尾与保全

- 容器 **10→10，运行0→0，新增/启动0**；候选Debian/Arch原登记CID匹配，登记文件哈希未变。Arch仍无合适CPU/RAM测试配额，未改旧容器。
- 本轮全部精确单元MainPID=0、负载cgroup已移除；失败单元的状态元数据保留，无自动重启。原测试slice TasksCurrent=0。没有显示、监听、真实包事务或客体进程。
- Git暂存条目一致；210个非本轮修改的源码/测试/工具/文档摘要一致；boot/KWin PID/start身份一致。源码修复前副本在 `before/`，本轮补丁在 `source.patch`，结束指纹在 `source-final.json`。
- 没有操作浏览器、活动桌面、日常容器、宿主设置或系统安装。容器前后清单一致；未采集连续事件流，日常文件未做全树摘要，不宣称所有外部事件或日常文件逐字节已核验。

剩余真实共享CLI、依赖安装/补库、Writer保存重开和Arch复核均未运行。详细32项状态见 `case-results.json`；原计划和历史回执未改写。本轮已结束，不自动重试PSI拒绝阶段。
'''
(out/'report.md').write_text(report)
print(json.dumps({'tests_passed':64,'minimum_host_available_gib':min_gib,'preservation':preservation,'report':str(out/'report.md')},ensure_ascii=False))
