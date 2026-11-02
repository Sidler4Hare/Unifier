"""Compatibility import for ``unifier.fuse_view``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.fuse_view", run_name="__main__")
else:
    from unifier import fuse_view as _module
    sys.modules[__name__] = _module
