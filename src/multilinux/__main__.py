"""Old ``python -m multilinux`` entry. Prefer ``python -m unifier``."""
import runpy

runpy.run_module("unifier", run_name="__main__")
