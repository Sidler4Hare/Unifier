"""Compatibility import for ``unifier.i18n``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.i18n", run_name="__main__")
else:
    from unifier import i18n as _module
    sys.modules[__name__] = _module
