"""Guest-only, opt-in aliases for known runtime libraries.

Published as a standalone script; never load guest libraries in the host agent.
The small private cache survives exec. Disabling the launch mode removes its
effect without deleting files that another application may still be using.
"""

import ctypes
import hashlib
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile


RULES = {
    "gtk3-native": {
        "alias": "libgtk-3.so",
        "soname": "libgtk-3.so.0",
        "symbols": (
            "gtk_file_chooser_native_new", "gtk_native_dialog_set_modal",
            "gtk_native_dialog_destroy", "gtk_native_dialog_show",
            "gtk_native_dialog_hide", "gtk_native_dialog_run",
        ),
    },
}


def elf_soname(path):
    """Read bounded ELF64 amd64 metadata, without executing the file."""
    with open(path, "rb") as file:
        size = os.fstat(file.fileno()).st_size

        def read(offset, count):
            if offset < 0 or count < 0 or offset + count > size:
                raise ValueError("truncated ELF metadata")
            file.seek(offset)
            data = file.read(count)
            if len(data) != count:
                raise ValueError("truncated ELF metadata")
            return data

        header = read(0, 64)
        if header[:7] != b"\x7fELF\x02\x01\x01":
            raise ValueError("runtime alias requires ELF64 little-endian")
        kind, machine = struct.unpack_from("<HH", header, 16)
        if kind != 3 or machine != 62:
            raise ValueError("runtime alias requires an amd64 shared library")
        phoff = struct.unpack_from("<Q", header, 32)[0]
        phsize, phnum = struct.unpack_from("<HH", header, 54)
        if phsize != 56 or not 0 < phnum <= 512:
            raise ValueError("unsupported ELF program headers")
        segments = [struct.unpack("<IIQQQQQQ", read(phoff + i * phsize, phsize))
                    for i in range(phnum)]
        dynamic = [p for p in segments if p[0] == 2]
        if len(dynamic) != 1 or dynamic[0][5] > 65536 or dynamic[0][5] % 16:
            raise ValueError("unsupported ELF dynamic table")
        tags = {}
        data = read(dynamic[0][2], dynamic[0][5])
        for offset in range(0, len(data), 16):
            tag, value = struct.unpack_from("<qQ", data, offset)
            if tag == 0:
                break
            if tag in (5, 10, 14):
                if tag in tags:
                    raise ValueError("ambiguous ELF dynamic metadata")
                tags[tag] = value
        if not all(tag in tags for tag in (5, 10, 14)):
            raise ValueError("library has no SONAME")
        if not 0 < tags[10] <= 16 * 1024 * 1024 or tags[14] >= tags[10]:
            raise ValueError("invalid ELF string table")
        tables = [p for p in segments if p[0] == 1 and p[3] <= tags[5]
                  and tags[5] + tags[10] <= p[3] + p[5]]
        if len(tables) != 1:
            raise ValueError("ELF string table is not uniquely file-backed")
        segment = tables[0]
        offset = segment[2] + tags[5] - segment[3] + tags[14]
        value = read(offset, min(256, tags[10] - tags[14]))
        if b"\0" not in value:
            raise ValueError("unterminated ELF SONAME")
        return value.split(b"\0", 1)[0].decode("ascii")


class DlInfo(ctypes.Structure):
    _fields_ = [("filename", ctypes.c_char_p), ("base", ctypes.c_void_p),
                ("symbol", ctypes.c_char_p), ("address", ctypes.c_void_p)]


def resolve_library(rule):
    """Resolve and validate in the guest, using that guest's own loader."""
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise ValueError("runtime alias requires a 64-bit guest")
    library = ctypes.CDLL(rule["soname"], mode=os.RTLD_NOW | os.RTLD_LOCAL)
    dladdr = ctypes.CDLL(None).dladdr
    dladdr.argtypes = [ctypes.c_void_p, ctypes.POINTER(DlInfo)]
    dladdr.restype = ctypes.c_int
    paths = set()
    for name in rule["symbols"]:
        symbol = getattr(library, name)
        info = DlInfo()
        if not dladdr(ctypes.cast(symbol, ctypes.c_void_p), ctypes.byref(info)) or not info.filename:
            raise ValueError(f"cannot resolve library for {name}")
        path = Path(os.fsdecode(info.filename)).resolve(strict=True)
        if not path.is_file():
            raise ValueError("native library is not a regular file")
        paths.add(path)
    if len(paths) != 1:
        raise ValueError("native symbols do not belong to one runtime library")
    path = paths.pop()
    if elf_soname(path) != rule["soname"]:
        raise ValueError("runtime library SONAME does not match the compatibility rule")
    return path


def private_directory(parent, name):
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        pass
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                         dir_fd=parent)
    info = os.fstat(descriptor)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(descriptor)
        raise ValueError("native alias cache must be owned by the guest user with mode 0700")
    return descriptor


def alias_environment(rule_name, environment, *, resolver=resolve_library, cache_root=None):
    """Create only an allowlisted alias; preserve all other launch settings."""
    if rule_name not in RULES:
        raise ValueError("unknown native library compatibility rule")
    rule = RULES[rule_name]
    target = Path(resolver(rule)).resolve(strict=True)
    info = target.stat()
    identity = f"{rule_name}\0{target}\0{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}"
    key = hashlib.sha256(identity.encode()).hexdigest()[:24]
    cache_root = Path(cache_root if cache_root is not None else tempfile.gettempdir()).resolve(strict=True)
    # Colon is a loader separator, and glibc expands dollar tokens in search
    # paths. Neither may enter the private search directory.
    if any(c in str(cache_root) for c in (":", "$", "\n", "\r")):
        raise ValueError("unsafe native alias cache path")
    name = f"unifier-native-{os.getuid()}"
    root = os.open(cache_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        private = private_directory(root, name)
        try:
            child = private_directory(private, key)
            try:
                try:
                    os.symlink(str(target), rule["alias"], dir_fd=child)
                except FileExistsError:
                    if os.readlink(rule["alias"], dir_fd=child) != str(target):
                        raise ValueError("existing native alias does not match the verified library")
            finally:
                os.close(child)
        finally:
            os.close(private)
    finally:
        os.close(root)
    env = dict(environment)
    directory = str(cache_root / name / key)
    existing = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = directory + (":" + existing if existing else "")
    return env


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) < 3 or args[1] != "--" or not args[2]:
            raise ValueError("expected a compatibility rule, --, and application argv")
        if os.environ.get("GTK_USE_PORTAL") != "1":
            raise ValueError("GTK Native compatibility requires GTK_USE_PORTAL=1")
        env = alias_environment(args[0], os.environ)
        os.execvpe(args[2], args[2:], env)
    except (OSError, ValueError, AttributeError) as exc:
        print(f"Unifier GTK Native compatibility failed: {exc}", file=sys.stderr)
        return 126


if __name__ == "__main__":
    sys.exit(main())
