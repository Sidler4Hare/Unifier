"""Compatibility import for ``unifier.views``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.views", run_name="__main__")
else:
    from unifier import views as _module
    sys.modules[__name__] = _module
