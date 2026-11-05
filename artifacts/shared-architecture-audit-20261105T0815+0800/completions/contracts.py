"""Remaining focused contracts; real package managers/containers are disabled."""
import concurrent.futures
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from test_packages import PackageBoxes, make_env, package_service
from test_dependencies import PluginBoxes
from unifier.runtime import ExecResult

OUT=Path(__file__).resolve().parent

def save(name,x): (OUT/name).write_text(json.dumps(x,ensure_ascii=False,indent=2))

class RemainingContracts(unittest.TestCase):
    def test_all_four_transaction_kinds_refuse_unknown_peer_before_write(self):
        results=[]
        for operation,names in [('install_packages',['hello']),('update_packages',None),('remove_packages',['hello']),('repair_dependencies',['hello'])]:
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                service,runtime=package_service(Path(directory))
                make_env(service,runtime,'debian13','a');make_env(service,runtime,'fedora44','b')
                runtime.read_inventory_readonly=None;runtime.events.clear()
                f=getattr(service,operation);txn=f('a') if names is None else f('a',names)
                results.append({'operation':operation,'phase':txn['phase'],'failure_phase':txn['failure_phase'],'native':txn['native'],'events':runtime.events})
                self.assertEqual(txn['failure_phase'],'isolation-preflight')
                self.assertEqual(txn['native'],[])
                self.assertFalse(txn['verification']['ok'])
                self.assertFalse(any(e[0] in ('start','stop','exec') for e in runtime.events))
        save('all-operation-admission.json',results)

    def test_failed_index_refresh_stops_dependency_repair_without_provider_query(self):
        class FailedIndex(PluginBoxes):
            def _answer(self,info,argv):
                if argv[:2]==['apt-file','update']:
                    return ExecResult(73,'','controlled index failure')
                return super()._answer(info,argv)
        with tempfile.TemporaryDirectory() as directory:
            service,runtime=package_service(Path(directory),FailedIndex())
            make_env(service,runtime,'debian13','a');runtime.events.clear()
            txn=service.install_packages('a',['vendor'],repair_dependencies=True)
            save('failed-index-transaction.json',txn)
            self.assertEqual(txn['phase'],'failed')
            self.assertEqual(txn['failure_phase'],'native-command')
            self.assertEqual(txn['dependency_repair']['status'],'unresolved')
            self.assertTrue(any(step['argv']==['apt-file','update'] and step['exit_code']==73 for step in txn['native']))
            self.assertFalse(any(e[0]=='exec' and e[2][:1]==('apt-file',) and e[2][1:2]!=('update',) for e in runtime.events))

    def test_two_same_environment_requests_have_one_native_writer(self):
        first=threading.Event();release=threading.Event();second=threading.Event()
        class SerialBoxes(PackageBoxes):
            def __init__(self):
                super().__init__();self.depth=0;self.maximum=0;self.entries=[]
            def exec(self,name,argv,**kwargs):
                native=argv[0]=='apt-get' and any(x in argv for x in ('serial-a','serial-b'))
                if native:
                    self.depth+=1;self.maximum=max(self.maximum,self.depth)
                    self.entries.append('serial-a' if 'serial-a' in argv else 'serial-b')
                    if 'serial-a' in argv:
                        first.set()
                        if not release.wait(4):raise RuntimeError('fixture release missing')
                try:return super().exec(name,argv,**kwargs)
                finally:
                    if native:self.depth-=1
        with tempfile.TemporaryDirectory() as directory:
            service,runtime=package_service(Path(directory),SerialBoxes())
            make_env(service,runtime,'debian13','a')
            def request_b():second.set();return service.install_packages('a',['serial-b'])
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                a=pool.submit(service.install_packages,'a',['serial-a'])
                self.assertTrue(first.wait(2))
                b=pool.submit(request_b)
                try:
                    self.assertTrue(second.wait(2));time.sleep(.1)
                    self.assertEqual(runtime.entries,['serial-a'])
                finally:release.set()
                txns=[a.result(timeout=8),b.result(timeout=8)]
            save('same-environment-writer-lock.json',{'max_simultaneous_writers':runtime.maximum,'entry_order':runtime.entries,'phases':[t['phase'] for t in txns]})
            self.assertEqual(runtime.maximum,1)
            self.assertEqual([t['phase'] for t in txns],['completed','completed'])

if __name__=='__main__':unittest.main()
