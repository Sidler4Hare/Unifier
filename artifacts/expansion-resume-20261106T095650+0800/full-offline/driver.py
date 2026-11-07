import json,os,subprocess,sys
from pathlib import Path
root=Path(__file__).resolve().parents[3];out=Path(__file__).resolve().parent
code=subprocess.run([sys.executable,str(root/'tools/check_repo.py')],cwd=root).returncode
(out/'repository-exit.json').write_text(json.dumps({'exit_code':code}))
env=dict(os.environ);env.update(HOME='/home/omg',XDG_DATA_HOME='/home/omg/.local/share',XDG_CONFIG_HOME='/home/omg/.config',XDG_RUNTIME_DIR='/run/user/1000')
recovery=subprocess.run([sys.executable,str(out/'recover.py')],cwd=root,env=env).returncode
raise SystemExit(code or recovery)
