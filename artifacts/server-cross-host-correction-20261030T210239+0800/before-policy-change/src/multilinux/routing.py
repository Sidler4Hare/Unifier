"""Compatibility import for ``unifier.routing``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.routing", run_name="__main__")
else:
    from unifier import routing as _module
    sys.modules[__name__] = _module
