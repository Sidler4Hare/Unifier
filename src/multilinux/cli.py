"""Compatibility import for ``unifier.cli``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.cli", run_name="__main__")
else:
    from unifier import cli as _module
    sys.modules[__name__] = _module
