"""Compatibility import for ``unifier.errors``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.errors", run_name="__main__")
else:
    from unifier import errors as _module
    sys.modules[__name__] = _module
