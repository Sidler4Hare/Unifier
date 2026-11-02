"""Compatibility import for ``unifier.view_broker``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.view_broker", run_name="__main__")
else:
    from unifier import view_broker as _module
    sys.modules[__name__] = _module
