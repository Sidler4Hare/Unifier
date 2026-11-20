"""Historical worker entry resolved by the object-anchored FUSE implementation."""

import sys

from unifier import fuse_view


if __name__ == "__main__":
    raise SystemExit(fuse_view.run_fuse(sys.argv[1:]))

sys.modules[__name__] = fuse_view
