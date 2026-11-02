"""Compatibility import for ``unifier.project_sessions``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.project_sessions", run_name="__main__")
else:
    from unifier import project_sessions as _module
    sys.modules[__name__] = _module
