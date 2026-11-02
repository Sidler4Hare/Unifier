"""Compatibility import for ``unifier.desktop_integrations``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.desktop_integrations", run_name="__main__")
else:
    from unifier import desktop_integrations as _module
    sys.modules[__name__] = _module
