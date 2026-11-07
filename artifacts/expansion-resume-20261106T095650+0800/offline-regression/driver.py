import importlib.util,io,json,subprocess,sys,unittest
from pathlib import Path
root=Path(__file__).resolve().parents[3];sys.path[:0]=[str(root/'tests'),str(root/'src'),str(root)]
import test_reuse_test_guard as tests
oldpath=Path(__file__).resolve().parents[1]/'before/tools/reuse_test_guard.py'
spec=importlib.util.spec_from_file_location('old_reuse_guard',oldpath);old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
current=tests.guard;tests.guard=old
case=tests.SupervisorContracts('test_parent_swap_cap_is_accepted_without_changing_container_limits')
buf=io.StringIO();r=unittest.TextTestRunner(stream=buf,verbosity=2).run(unittest.TestSuite([case]));tests.guard=current
assert not r.wasSuccessful() and len(r.errors)==1,buf.getvalue()
Path(__file__).with_name('before.log').write_text(buf.getvalue())
print('Confirmed old single-layer check rejects the safe parent-limited fixture',flush=True)
p=subprocess.run([sys.executable,'-m','unittest','-v','test_reuse_guard_policy','test_reuse_test_guard','test_package_observation','test_package_coordination','test_application_lifecycle'],cwd=root,check=False)
sys.exit(p.returncode)
