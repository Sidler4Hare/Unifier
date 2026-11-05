"""Diagnostic probes: green means the reported limitation was reproduced."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from test_packages import PackageBoxes, make_env, package_service
from test_on_demand import ready
from test_dependencies import PluginBoxes
from unifier.dependencies import SCAN, CACHE
from unifier.on_demand import LaunchGroup, managed_open
from unifier.runtime import ExecResult, PodmanRuntime

OUT = Path(__file__).resolve().parent

def save(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2))


class ArchitectureProbes(unittest.TestCase):
    def test_adding_a_second_distro_blocks_previously_working_package_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            service, runtime = package_service(Path(directory))
            runtime.read_inventory_readonly = None  # Actual PodmanRuntime has no reader.
            make_env(service, runtime, 'debian13', 'a')
            before = service.install_packages('a', ['hello'])
            make_env(service, runtime, 'fedora44', 'b')
            runtime.events.clear()
            after = service.install_packages('a', ['hello2'])
            save('cross-environment-admission.json', {
                'actual_podman_readonly_capability': callable(getattr(PodmanRuntime, 'read_inventory_readonly', None)),
                'single_env_phase': before['phase'], 'two_env_phase': after['phase'],
                'failure_phase': after['failure_phase'], 'native': after['native'],
                'error': after['native_error'], 'runtime_events': runtime.events})
            self.assertEqual(before['phase'], 'completed')
            self.assertEqual(after['failure_phase'], 'isolation-preflight')
            self.assertEqual(after['native'], [])
            self.assertFalse(any(e[0] in ('start', 'stop', 'exec') for e in runtime.events))

    def test_independent_concurrent_writes_are_both_reported_as_isolation_failures(self):
        gate_before = threading.Barrier(2)
        gate_after = threading.Barrier(2)
        class InterleavedBoxes(PackageBoxes):
            def exec(self, name, argv, **kwargs):
                native = argv[0] in ('apt-get', 'dnf') and any(x in argv for x in ('probe-a', 'probe-b'))
                if native:
                    gate_before.wait(timeout=8)
                result = super().exec(name, argv, **kwargs)
                if native:
                    gate_after.wait(timeout=8)
                return result
        with tempfile.TemporaryDirectory() as directory:
            service, runtime = package_service(Path(directory), InterleavedBoxes())
            make_env(service, runtime, 'debian13', 'a')
            make_env(service, runtime, 'fedora44', 'b')
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                a = pool.submit(service.install_packages, 'a', ['probe-a'])
                b = pool.submit(service.install_packages, 'b', ['probe-b'])
                transactions = [a.result(timeout=20), b.result(timeout=20)]
            save('concurrent-peer-false-positive.json', {
                'scope': 'two fake environments in one bounded offline process; not real concurrent containers',
                'transactions': transactions,
                'installed': {k: sorted(v) for k,v in runtime.installed.items()}})
            self.assertEqual([t['failure_phase'] for t in transactions], ['verification','verification'])
            self.assertIn('probe-a', runtime.installed['a'])
            self.assertIn('probe-b', runtime.installed['b'])
            self.assertTrue(all(t['verification'].get('isolation') for t in transactions))

    def test_app_worker_release_does_not_account_for_a_background_child(self):
        with tempfile.TemporaryDirectory() as directory:
            service, runtime = ready(Path(directory))
            runtime.events.clear()  # Exclude the fixture's non-JSON CreateSpec setup event.
            children = []
            def daemonizing_launcher(*args, **kwargs):
                child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(15)'],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
                children.append(child)
                return ExecResult(0, '', '')
            service._open_app_unmanaged = daemonizing_launcher
            try:
                managed_open(service, 'notes', [])
                child = children[0]
                state = service.inspect('debian13')['runtime_state']
                save('background-app-lease.json', {
                    'scope': 'host-owned child models a daemonized guest app; container runtime is fake',
                    'child_pid': child.pid, 'child_alive_after_launcher': child.poll() is None,
                    'environment_state': state,
                    'leases': LaunchGroup(service, 'debian13').read()['leases'],
                    'runtime_events': runtime.events})
                self.assertIsNone(child.poll())
                self.assertEqual(state, 'stopped')
                self.assertEqual(LaunchGroup(service, 'debian13').read()['leases'], {})
            finally:
                for child in children:
                    if child.poll() is None:
                        # Exact Popen child, still unreaped; PID cannot be reused here.
                        child.terminate()
                    child.wait(timeout=3)

    def test_bundled_soname_can_pass_static_repair_but_fail_actual_loader(self):
        tools = {name: shutil.which(name) for name in ('cc','readelf','ldconfig')}
        if not all(tools.values()):
            self.skipTest('native ELF fixture tools missing')
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory); libdir=base/'private-unsearched';libdir.mkdir()
            soname='libunifier_arch_probe_1105.so.1'
            (base/'library.c').write_text('int unifier_probe(void) {return 42;}\n')
            (base/'main.c').write_text('extern int unifier_probe(void); int main(void) {return unifier_probe()!=42;}\n')
            lib=libdir/soname; app=base/'app'
            subprocess.run([tools['cc'],'-shared','-fPIC',str(base/'library.c'),'-Wl,-soname,'+soname,'-o',str(lib)],check=True,timeout=30)
            subprocess.run([tools['cc'],str(base/'main.c'),'-L'+str(libdir),'-l:'+soname,'-o',str(app)],check=True,timeout=30)
            objects=[app,lib]
            scan=''.join('UNIFIER_ELF\t'+str(p)+'\n'+subprocess.check_output([tools['readelf'],'-W','-h','-d',str(p)],text=True) for p in objects)+'UNIFIER_SCAN_DONE\n'
            cache=subprocess.check_output([tools['ldconfig'],'-p'],text=True)
            class BundledBoxes(PluginBoxes):
                def _answer(self, info, argv):
                    if argv[:2]==['dpkg-query','-L']:
                        return ExecResult(0,'\n'.join(str(p) for p in objects)+'\n','')
                    if argv[:3]==['/bin/sh','-c',SCAN]:return ExecResult(0,scan,'')
                    if argv[:3]==['/bin/sh','-c',CACHE]:return ExecResult(0,cache,'')
                    return super()._answer(info,argv)
            service,runtime=package_service(base/'fake-root',BundledBoxes())
            make_env(service,runtime,'debian13','a')
            txn=service.install_packages('a',['vendor'],repair_dependencies=True)
            env=dict(os.environ)
            env.pop('LD_LIBRARY_PATH',None);env.pop('LD_PRELOAD',None)
            actual=subprocess.run([str(app)],env=env,capture_output=True,text=True,timeout=5)
            save('bundled-vs-loader.json',{'scope':'real host-built private ELF loader fixture plus fake package database; no guest library copying',
                'transaction_phase':txn['phase'],'dependency_repair':txn['dependency_repair'],
                'actual_loader_exit':actual.returncode,'actual_loader_error':actual.stderr,
                'hashes':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in objects},
                'fixture_sources':{n:(base/n).read_text() for n in ('library.c','main.c')},'scan':scan})
            self.assertEqual(txn['phase'],'completed')
            self.assertEqual(txn['dependency_repair']['runtime_loading'],'unverified')
            self.assertIn(soname,txn['dependency_repair']['bundled_candidates'])
            self.assertNotEqual(actual.returncode,0)
            self.assertIn(soname,actual.stderr)


if __name__ == '__main__':
    unittest.main()
