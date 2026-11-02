"""Compatibility import for ``unifier.runtime``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.runtime", run_name="__main__")
else:
    from unifier import runtime as _module
    sys.modules[__name__] = _module
