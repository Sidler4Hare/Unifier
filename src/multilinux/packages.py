"""Compatibility import for ``unifier.packages``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.packages", run_name="__main__")
else:
    from unifier import packages as _module
    sys.modules[__name__] = _module
