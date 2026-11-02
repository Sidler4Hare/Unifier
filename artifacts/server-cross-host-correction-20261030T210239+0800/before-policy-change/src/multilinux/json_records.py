"""Compatibility import for ``unifier.json_records``."""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("unifier.json_records", run_name="__main__")
else:
    from unifier import json_records as _module
    sys.modules[__name__] = _module
