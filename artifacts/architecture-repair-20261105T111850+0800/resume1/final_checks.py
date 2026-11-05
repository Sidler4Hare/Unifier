import os
import subprocess
import sys
subprocess.run([sys.executable, "tools/check_repo.py", "--quick"], check=True)
subprocess.run([sys.executable, "-m", "unittest", "-v", "test_runtime_control", "test_project_sessions", "test_session_stopped_recovery", "test_session_recovery_contract", "test_cli_shortcuts"], check=True)
