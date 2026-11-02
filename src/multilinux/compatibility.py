"""Compatibility import for ``unifier.compatibility``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.compatibility", run_name="__main__")
else:
    from unifier import compatibility as _module
    sys.modules[__name__] = _module
