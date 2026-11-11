"""The file selection subset of Zenity's command line."""

import re

if __package__:
    from .kdialog_args import DialogRequest
else:
    from kdialog_args import DialogRequest


_FLAGS = {"--file-selection", "--multiple", "--directory", "--save",
          "--modal", "--confirm-overwrite"}
_VALUES = {"--title", "--filename", "--separator", "--file-filter", "--attach"}


def parse_args(argv):
    if len(argv) > 32 or any(len(arg) > 4096 or "\0" in arg for arg in argv):
        raise ValueError("Zenity arguments exceed the supported bounds")
    if argv == ["--version"]:
        return None
    options = {}
    filters = []
    args = iter(argv)
    for arg in args:
        key, equal, value = arg.partition("=")
        if key in _VALUES:
            value = value if equal else next(args, None)
            if value is None or (not equal and value.startswith("--")):
                raise ValueError(f"{key} requires a value")
        elif key not in _FLAGS or equal:
            raise ValueError("unsupported Zenity file selection option")
        if key == "--file-filter":
            label, bar, patterns = value.partition("|")
            globs = tuple((patterns if bar else label).split())
            if (not globs or any("/" in glob or "|" in glob for glob in globs)
                    or (bar and not label.strip())):
                raise ValueError("invalid Zenity file filter")
            filters.append((label.strip(), globs))
            continue
        if key in options:
            raise ValueError(f"duplicate Zenity option: {key}")
        options[key] = value
    if "--file-selection" not in options:
        raise ValueError("only Zenity file selection is supported")
    folder = "--directory" in options
    save = "--save" in options
    multiple = "--multiple" in options
    if (save and (folder or multiple)) or ("--confirm-overwrite" in options and not save):
        raise ValueError("incompatible Zenity file selection options")
    separator = options.get("--separator", "|")
    if not separator or len(separator) > 16 or ("--separator" in options and not multiple):
        raise ValueError("Zenity separator requires multiple selection")
    parent = ""
    if "--attach" in options:
        xid = options["--attach"]
        if not re.fullmatch(r"[0-9]{1,10}|0[xX][0-9a-fA-F]{1,8}", xid):
            raise ValueError("Zenity parent must be an X11 window ID")
        number = int(xid, 16 if xid.lower().startswith("0x") else 10)
        if number > 0xffffffff:
            raise ValueError("Zenity parent must be an X11 window ID")
        if number:
            parent = f"x11:{number:x}"
    return DialogRequest("save" if save else "folder" if folder else "open",
                         options.get("--title", ""), parent, multiple,
                         options.get("--filename", "."), tuple(filters),
                         separator=separator, modal="--modal" in options)
