"""Compatibility import for ``unifier.file_operations``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.file_operations", run_name="__main__")
else:
    from unifier import file_operations as _module
    sys.modules[__name__] = _module
