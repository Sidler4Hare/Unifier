"""Compatibility import for ``unifier.atomic_io``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.atomic_io", run_name="__main__")
else:
    from unifier import atomic_io as _module
    sys.modules[__name__] = _module
