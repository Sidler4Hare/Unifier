"""Compatibility import for ``unifier.share_label``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.share_label", run_name="__main__")
else:
    from unifier import share_label as _module
    sys.modules[__name__] = _module
