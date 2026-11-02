"""Compatibility import for ``unifier.root_identity``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.root_identity", run_name="__main__")
else:
    from unifier import root_identity as _module
    sys.modules[__name__] = _module
