"""Compatibility import for ``unifier.view_backend``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.view_backend", run_name="__main__")
else:
    from unifier import view_backend as _module
    sys.modules[__name__] = _module
