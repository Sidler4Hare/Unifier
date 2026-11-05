"""Bounded T0 admission observation only; never imports or starts Unifier."""
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import time
from datetime import datetime
from zoneinfo import ZoneInfo

resource.setrlimit(resource.RLIMIT_AS, (128 * 1024**2, 128 * 1024**2))
resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024**2, 2 * 1024**2))
resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
ROOT = Path.cwd()
OUT = Path(__file__).resolve().parent
TZ = ZoneInfo('Asia/Shanghai')
GIB = 1024**3

def stamp():
    return datetime.now(TZ).isoformat()

def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

def command(args):
    result = subprocess.run(args, cwd=ROOT, env={**os.environ, 'GOMAXPROCS': '1', 'GIT_OPTIONAL_LOCKS': '0'},
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    if len(result.stdout) + len(result.stderr) > 1024**2:
        raise RuntimeError('T0 output budget exceeded')
    if result.returncode:
        raise RuntimeError(f'{args[0]} exit {result.returncode}: {result.stderr.decode(errors="replace")[:300]}')
    return result.stdout

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(65536), b''):
            h.update(block)
    return h.hexdigest()

def inventory():
    raw = command(['podman', 'ps', '-a', '--no-trunc', '--format', '{{.ID}} {{.Names}} {{.State}}']).decode()
    return [dict(zip(('id', 'name', 'state'), line.split())) for line in raw.splitlines()]

def kwin_identity():
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            if entry.stat().st_uid != os.getuid() or (entry / 'comm').read_text().strip() not in ('kwin_wayland', 'kwin_x11'):
                continue
            raw = (entry / 'stat').read_text()
            found.append({'pid': int(entry.name), 'starttime': raw.rsplit(')', 1)[1].split()[19]})
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return found

def snapshot():
    names = [ROOT / n for n in ('AGENTS.md', 'README.md', 'HANDOFF.md')]
    for directory in ('src', 'tests', 'tools', 'docs'):
        names.extend(p for p in (ROOT / directory).rglob('*') if p.is_file() and not p.is_symlink() and '__pycache__' not in p.parts)
    return {'time': stamp(), 'head': command(['git', 'rev-parse', 'HEAD']).decode().strip(),
            'index_entries_sha256': hashlib.sha256(command(['git', 'ls-files', '--stage', '-z'])).hexdigest(),
            'tracked_status': command(['git', 'status', '--short', '--untracked-files=no']).decode(),
            'files': {str(p.relative_to(ROOT)): digest(p) for p in sorted(set(names))},
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'kwin': kwin_identity(), 'containers': inventory(),
            'test_slice': command(['systemctl', '--user', 'show', 'unifier-tests.slice', '-p', 'ControlGroup',
                                  '-p', 'TasksCurrent', '-p', 'MemoryCurrent', '-p', 'MemoryMax', '-p', 'TasksMax']).decode(),
            'installed_entry': str((Path.home() / '.local/bin/unifier').resolve()),
            'disk': dict(zip(('total', 'used', 'free'), shutil.disk_usage(ROOT)))}

def sample():
    memory = {k: int(v.split()[0]) * 1024 for k, v in (line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())}
    pressure = {}
    for line in Path('/proc/pressure/memory').read_text().splitlines():
        words = line.split()
        pressure[words[0]] = {k: float(v) for k, v in (part.split('=') for part in words[1:])}
    return {'time': stamp(), 'monotonic': time.monotonic(), 'mem_total': memory['MemTotal'],
            'mem_available': memory['MemAvailable'], 'swap_total': memory['SwapTotal'],
            'swap_free': memory['SwapFree'], 'psi': pressure}

started = stamp()
before = snapshot()
save('before.json', before)
# A conservative example T1 budget; no load is admitted or launched by this sampler.
budget = 512 * 1024**2
monitor_budget = 64 * 1024**2
required = 2 * GIB + budget + monitor_budget + max(128 * 1024**2, int(budget * .1))
samples = []
start = time.monotonic()
with (OUT / 'host-window.jsonl').open('w') as stream:
    for index in range(121):
        time.sleep(max(0, start + index * .25 - time.monotonic()))
        row = sample()
        samples.append(row)
        stream.write(json.dumps(row) + '\n')
        stream.flush()
after = snapshot()
save('after.json', after)
minimum = min(r['mem_available'] for r in samples)
maximum = max(r['mem_available'] for r in samples)
window_ok = all(r['mem_available'] >= required and r['psi']['some']['avg10'] < 2 and r['psi']['full']['avg10'] < .5 for r in samples)
plan = json.loads((ROOT / 'artifacts/shared-container-dependency-plan-20261104/plan.json').read_text())
# Do not alter the published plan or its initial NOT_RUN states.
result = {'status': 'BLOCKED', 'started_at': started, 'finished_at': stamp(), 'mode': 'T0_only_no_product_execution',
          'source_plan': 'artifacts/shared-container-dependency-plan-20261104/plan.json',
          'resource_window': {'samples': len(samples), 'duration_seconds': samples[-1]['monotonic'] - samples[0]['monotonic'],
                              'min_available_bytes': minimum, 'max_available_bytes': maximum,
                              'reserve_bytes': 2 * GIB, 'example_t1_budget_bytes': budget,
                              'monitor_budget_bytes': monitor_budget, 'required_bytes': required,
                              'low_pressure_and_memory_window_passed': window_ok,
                              'max_some_avg10': max(r['psi']['some']['avg10'] for r in samples),
                              'max_full_avg10': max(r['psi']['full']['avg10'] for r in samples)},
          'blockers': ['PRE03: no qualifying resource window; do not launch T1/model/container workload' if not window_ok else 'PRE03: compliant independent supervision still unavailable',
                       'PRE02: service.py:1825-1835 still starts stopped peer for package inventory; static confirmation only',
                       'PRE03: old HostReserveMonitor uses in-process daemon thread, 1s polling, lacks PSI and external liveness guard'],
          'tests_executed': 0, 'models_launched': 0, 'containers_created': 0,
          'product_code_modified': False, 'deployed': False, 'workflow_acceptance': 'NOT_RUN',
          'preservation': {'source_docs_hashes_equal': before['files'] == after['files'],
                           'index_entries_equal': before['index_entries_sha256'] == after['index_entries_sha256'],
                           'tracked_status_equal': before['tracked_status'] == after['tracked_status'],
                           'container_list_states_equal': before['containers'] == after['containers'],
                           'container_count_before': len(before['containers']), 'container_count_after': len(after['containers']),
                           'running_before': sum(c['state'] == 'running' for c in before['containers']),
                           'running_after': sum(c['state'] == 'running' for c in after['containers']),
                           'boot_equal': before['boot_id'] == after['boot_id'], 'kwin_identity_equal': before['kwin'] == after['kwin']},
          'cleanup': {'product_processes_started': False, 'test_units_started': False,
                      'display_or_listener_started': False, 'package_transaction_started': False,
                      'residual_objects': 'only this evidence directory; no background workers created',
                      'not_verified': ['continuous Podman event stream', 'daily user files byte-for-byte', 'full CID ownership/UUID/generation and original registration', 'container live cgroups (all stopped)', 'live package recovery state']}}
save('result.json', result)
report = f'''# 共享容器与依赖专项测试启动回执\n\n时间：{started} 至 {result['finished_at']}（Asia/Shanghai）。状态：**BLOCKED**。本轮仅完成 T0 只读准入观察，产品测试执行数为 **0**。\n\n按 2026-11-04 最新专项计划从 PRE02/PRE03 开始；读取了最新 WPS 对照报告及现行全项目规范。没有启动模型、应用、容器、包事务或回归套件。\n\n## 准入结果\n\n- 30 秒窗口共 {len(samples)} 个采样点；MemAvailable 为 **{minimum/GIB:.3f}–{maximum/GIB:.3f} GiB**；宿主保留线 2 GiB。\n- 保守离线批次：512 MiB 作业 + 64 MiB 独立保护 + 128 MiB 余量，所需 MemAvailable 至少 **{required/GIB:.4f} GiB**；整个窗口满足准入：{window_ok}。这是待执行预算，并非已验证的保护部署。\n- memory PSI avg10 最高 some={result['resource_window']['max_some_avg10']}%、full={result['resource_window']['max_full_avg10']}%。\n- PRE02：当前 `src/unifier/service.py:1825` 仍会 start 停止的同伴容器，读清单后 stop；仅静态确认，离线复现与实机复核均 NOT_RUN。相关实机包事务继续阻断。\n- PRE03：旧保护器仍为同进程 daemon 线程、1 秒采样，无 PSI/外部存活监督；不能作为新实机测试完整保护。\n- PRE01：当前只读清单 {len(before['containers'])}→{len(after['containers'])} 个，运行数 {result['preservation']['running_before']}→{result['preservation']['running_after']}；候选完整 CID/UUID/generation/原登记尚未完成核对，未准入任何实例。PRE04/PRE05 及 SH/DEP/AR 全部 NOT_RUN。\n\n## 收尾与保全\n\n源码/测试/工具/文档哈希一致：{result['preservation']['source_docs_hashes_equal']}；Git 暂存条目一致：{result['preservation']['index_entries_equal']}；既有 tracked 脏状态一致：{result['preservation']['tracked_status_equal']}。boot/KWin 身份一致：{result['preservation']['boot_equal']}/{result['preservation']['kwin_identity_equal']}。\n\n未启动测试进程、线程、systemd 单元、显示或监听；本 T0 采样器同步结束，由外层等待退出码。没有安装/配置变更、包恢复动作或部署。没有清理、重启用户应用或更改宿主设置。只新增本证据目录。END01 的本轮对象收尾范围完成，不代表产品生命周期通过。\n\n容器前后清单一致；未采集连续事件流，因此不声称证明期间无任何外部事件。日常数据未做全树摘要，不能声称逐字节核验。停止态客体的真实 cgroup、所有权、租约和包恢复未验收。\n\n## 继续条件\n\n先满足预算对应的连续 30 秒资源窗口，再在整棵进程树硬预算、独立监测和总期限下执行 PRE02 离线复现/修正，以及 PRE03 保护契约；实际保护和固定池身份通过后方可进入真实 CLI/包/GUI。保护要求不放宽，本轮没有自动重试。\n\n证据：`before.json`、`after.json`、`host-window.jsonl`、`result.json`、`readonly_preflight.py`。历史计划及报告未改写。\n'''
(OUT / 'report.md').write_text(report)
print(json.dumps({'status': result['status'], 'min_available_gib': minimum/GIB, 'max_available_gib': maximum/GIB,
                  'admission': window_ok, 'tests_executed': 0, 'preservation': result['preservation'], 'report': str(OUT / 'report.md')}, ensure_ascii=False))
