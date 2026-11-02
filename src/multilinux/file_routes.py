"""Compatibility import for ``unifier.file_routes``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.file_routes", run_name="__main__")
else:
    from unifier import file_routes as _module
    sys.modules[__name__] = _module
