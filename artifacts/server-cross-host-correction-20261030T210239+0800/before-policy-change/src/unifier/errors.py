"""Errors the lifecycle CLI can report without a traceback."""


class UnifierError(RuntimeError):
    pass

# Old import name. Callers and the compatibility package can still use it.
MultilinuxError = UnifierError
