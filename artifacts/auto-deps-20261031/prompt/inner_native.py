import sys; sys.path.insert(0,'src')
from unifier.native_io import run_native
r=run_native(sys.argv[1:],interactive=True,stream=False,timeout=60); sys.exit(r.returncode)
