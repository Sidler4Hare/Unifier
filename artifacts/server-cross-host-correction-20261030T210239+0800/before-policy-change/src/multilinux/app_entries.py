"""Compatibility import for ``unifier.app_entries``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.app_entries", run_name="__main__")
else:
    from unifier import app_entries as _module
    sys.modules[__name__] = _module
