"""Compatibility import for ``unifier.guest_paths``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.guest_paths", run_name="__main__")
else:
    from unifier import guest_paths as _module
    sys.modules[__name__] = _module
