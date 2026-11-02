"""Compatibility import for ``unifier.view_journal``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.view_journal", run_name="__main__")
else:
    from unifier import view_journal as _module
    sys.modules[__name__] = _module
