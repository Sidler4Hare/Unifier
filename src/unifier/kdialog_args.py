"""Parse the subset of kdialog used by Chromium's Linux file chooser."""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class DialogRequest:
    mode: str
    title: str
    parent: str
    multiple: bool
    start: str
    filters: tuple[tuple[str, tuple[str, ...]], ...]
    separator: str = "\n"
    modal: bool = True


_MODES = {"--getopenfilename": "open", "--getexistingdirectory": "folder",
          "--getsavefilename": "save"}
_MIME_TYPE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/(?:[A-Za-z0-9!#$&^_.+-]+|\*)")


def _filters(text):
    if not text:
        return ()
    mimes = text.split()
    if mimes and all(_MIME_TYPE.fullmatch(mime) for mime in mimes):
        return tuple((mime, (mime,)) for mime in mimes)
    entries = []
    for item in text.split("|"):
        match = re.fullmatch(r"(.+?) \(([^()]+)\)", item)
        if match is None:
            raise ValueError("unsupported kdialog filter syntax")
        label, patterns = match.groups()
        globs = tuple(patterns.split())
        if not globs or any("/" in pattern or not pattern.startswith("*") for pattern in globs):
            raise ValueError("unsupported kdialog filter pattern")
        entries.append((label, globs))
    return tuple(entries)


def parse_args(argv):
    if len(argv) > 24 or any(len(arg) > 4096 or "\0" in arg for arg in argv):
        raise ValueError("kdialog arguments exceed the supported bounds")
    if argv == ["--version"]:
        return None
    switches = {}
    positionals = []
    parse_switches = True
    args = iter(argv)
    for arg in args:
        if parse_switches and arg == "--":
            parse_switches = False
        elif parse_switches and arg.startswith("--"):
            key, equal, value = arg.partition("=")
            if key in ("--attach", "--embed", "--title"):
                value = value if equal else next(args, "")
                if key != "--title":
                    key = "--parent"
                if not value:
                    raise ValueError(f"{key} requires a value")
            elif equal or (key not in _MODES and key not in ("--multiple", "--separate-output")):
                raise ValueError("unsupported kdialog switch")
            if key in switches:
                raise ValueError(f"duplicate kdialog switch: {key}")
            switches[key] = value
        else:
            parse_switches = False
            positionals.append(arg)
    modes = [mode for key, mode in _MODES.items() if key in switches]
    if len(modes) != 1 or not 1 <= len(positionals) <= (1 if modes == ["folder"] else 2):
        raise ValueError("expected one kdialog file operation and a start path")
    mode = modes[0]
    multiple = "--multiple" in switches
    if multiple != ("--separate-output" in switches) or (multiple and mode != "open"):
        raise ValueError("kdialog multiple selection requires separate output and open mode")
    parent = ""
    if "--parent" in switches:
        xid = switches["--parent"]
        if not re.fullmatch(r"[0-9]{1,10}", xid) or int(xid) > 0xffffffff:
            raise ValueError("kdialog parent must be an X11 window ID")
        if int(xid):
            parent = f"x11:{int(xid):x}"
    return DialogRequest(mode, switches.get("--title", ""), parent, multiple,
                         positionals[0], _filters(positionals[1]) if len(positionals) == 2 else ())
