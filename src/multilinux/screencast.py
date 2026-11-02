"""Compatibility import for ``unifier.screencast``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.screencast", run_name="__main__")
else:
    from unifier import screencast as _module
    sys.modules[__name__] = _module
