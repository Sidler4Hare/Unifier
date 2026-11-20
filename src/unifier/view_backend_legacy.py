"""Historical import for the object-anchored view backend."""

import sys

from unifier import view_backend


if __name__ != "__main__":
    sys.modules[__name__] = view_backend
