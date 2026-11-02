"""Compatibility import for ``unifier.project``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.project", run_name="__main__")
else:
    from unifier import project as _module
    sys.modules[__name__] = _module
