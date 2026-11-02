"""Compatibility import for ``unifier.catalog``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.catalog", run_name="__main__")
else:
    from unifier import catalog as _module
    sys.modules[__name__] = _module
