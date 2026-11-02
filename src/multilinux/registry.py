"""Compatibility import for ``unifier.registry``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.registry", run_name="__main__")
else:
    from unifier import registry as _module
    sys.modules[__name__] = _module
