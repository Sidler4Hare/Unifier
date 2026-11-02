"""Compatibility import for ``unifier.install_state``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.install_state", run_name="__main__")
else:
    from unifier import install_state as _module
    sys.modules[__name__] = _module
