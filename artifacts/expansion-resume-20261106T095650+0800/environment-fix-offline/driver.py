import subprocess,sys
from pathlib import Path
root=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(root/'src'),str(root/'tests')]
raise SystemExit(subprocess.run([sys.executable,'-m','unittest','-v','test_exec_control_empty_arguments','test_runtime_control','test_application_lifecycle','test_application_supervisor','test_on_demand','test_app_user'],cwd=root).returncode)
