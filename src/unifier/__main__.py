"""Installed entry point: shortcuts and diagnostics share one user root."""
import os
from unifier.cli import main
from unifier.install import installation_root

os.environ.setdefault("UNIFIER_HOME", str(installation_root()))

raise SystemExit(main())
