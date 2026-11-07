import importlib.util,json,os,subprocess,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3];OUT=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests'),str(ROOT)]
import test_runtime_control as regression
oldpath=OUT.parent/'before-env-fix/src/unifier/runtime.py'
spec=importlib.util.spec_from_file_location('before_guest_env_fix',oldpath)
old=importlib.util.module_from_spec(spec);sys.modules[spec.name]=old;spec.loader.exec_module(old)
current=regression.PodmanRuntime
regression.PodmanRuntime=old.PodmanRuntime
try:
    suite=unittest.TestSuite([regression.RealHandshake(
        'test_guest_home_and_runtime_do_not_redirect_the_host_client')])
    # setUpClass normally belongs to the full suite runner.
    regression.RealHandshake.setUpClass()
    try:
        result=unittest.TestResult();suite.run(result)
        assert result.testsRun==1 and len(result.failures)==1 and not result.errors
        (OUT/'before-fix-failure.txt').write_text(result.failures[0][1])
        print('Reproduced old client HOME pollution with the same regression.',flush=True)
    finally:regression.RealHandshake.tearDownClass()
finally:regression.PodmanRuntime=current
code=subprocess.run([sys.executable,str(ROOT/'tools/check_repo.py')],cwd=ROOT).returncode
(OUT/'repository-exit.json').write_text(json.dumps({'exit_code':code}))
environment=dict(os.environ)
environment.update(HOME='/home/omg',XDG_DATA_HOME='/home/omg/.local/share',
    XDG_CONFIG_HOME='/home/omg/.config',XDG_RUNTIME_DIR='/run/user/1000')
audit=subprocess.run([sys.executable,str(OUT/'audit.py')],cwd=ROOT,env=environment).returncode
raise SystemExit(code or audit)
