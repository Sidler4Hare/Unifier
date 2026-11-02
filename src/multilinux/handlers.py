"""Compatibility import for ``unifier.handlers``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.handlers", run_name="__main__")
else:
    from unifier import handlers as _module
    sys.modules[__name__] = _module
