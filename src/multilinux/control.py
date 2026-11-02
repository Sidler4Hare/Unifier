"""Compatibility import for ``unifier.control``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.control", run_name="__main__")
else:
    from unifier import control as _module
    sys.modules[__name__] = _module
