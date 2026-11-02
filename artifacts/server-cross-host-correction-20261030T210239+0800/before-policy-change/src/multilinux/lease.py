"""Compatibility import for ``unifier.lease``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.lease", run_name="__main__")
else:
    from unifier import lease as _module
    sys.modules[__name__] = _module
