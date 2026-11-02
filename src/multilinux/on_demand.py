"""Compatibility import for ``unifier.on_demand``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.on_demand", run_name="__main__")
else:
    from unifier import on_demand as _module
    sys.modules[__name__] = _module
