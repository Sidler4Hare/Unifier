import importlib.util,subprocess,sys
from pathlib import Path
root=Path(__file__).resolve().parents[3];sys.path[:0]=[str(root/'src'),str(root)]
from unifier.runtime import application_guest_argv,exec_argv,CONTROL_FD
oldpath=Path(__file__).resolve().parents[1]/'before/src/unifier/exec_control.py'
spec=importlib.util.spec_from_file_location('old_exec',oldpath);old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
command=exec_argv('fixed-cid',application_guest_argv('run','a'*32,CONTROL_FD,'/bin/true'),workdir=None,env={},control_fd=CONTROL_FD)
try:old.control_exec_argv(4,command)
except ValueError as exc:print('Reproduced pre-spawn default APP failure:',exc,flush=True)
else:raise AssertionError('old failure not reproduced')
raise SystemExit(subprocess.run([sys.executable,'-m','unittest','-v','test_exec_control_empty_arguments','test_runtime_control','test_application_lifecycle','test_application_supervisor','test_on_demand'],cwd=root).returncode)
