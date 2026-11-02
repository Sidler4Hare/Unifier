"""Compatibility import for ``unifier.identity``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.identity", run_name="__main__")
else:
    from unifier import identity as _module
    sys.modules[__name__] = _module
