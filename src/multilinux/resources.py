"""Compatibility import for ``unifier.resources``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.resources", run_name="__main__")
else:
    from unifier import resources as _module
    sys.modules[__name__] = _module
