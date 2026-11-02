"""Compatibility import for ``unifier.projects``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.projects", run_name="__main__")
else:
    from unifier import projects as _module
    sys.modules[__name__] = _module
