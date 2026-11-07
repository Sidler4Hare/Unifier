"""Build Debian fixture packages on the host. Does not start a container."""
import hashlib, io, json, os, stat, subprocess, tarfile, time, urllib.request
from pathlib import Path

ROOT = Path("/home/omg/文档/多兼容linux机/artifacts/expansion-resume-20261107T222500+0800")
CACHE = ROOT / "cache"
OUT = ROOT / "debs"
MIRROR = "https://deb.debian.org/debian/"
CACHE.mkdir(exist_ok=True)
OUT.mkdir(exist_ok=True)

FILES = {
    "libestr0": "pool/main/libe/libestr/libestr0_0.1.11-2_amd64.deb",
    "libfastjson4": "pool/main/libf/libfastjson/libfastjson4_1.2304.0-2_amd64.deb",
    "libconfig-dev": "pool/main/libc/libconfig/libconfig-dev_1.7.3-2_amd64.deb",
    "libcapstone5": "pool/main/c/capstone/libcapstone5_5.0.7-1~deb13u1_amd64.deb",
}

def fetch(name, filename):
    dest = CACHE / name / Path(filename).name
    dest.parent.mkdir(exist_ok=True)
    if not dest.exists() or dest.stat().st_size < 1000:
        urllib.request.urlretrieve(MIRROR + filename, dest)
    return dest

def extract(deb, dest):
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ar", "x", "--output", str(dest), str(deb)], check=True)
    data = next(dest.glob("data.tar.*"))
    root = dest / "root"
    root.mkdir(exist_ok=True)
    subprocess.run(["tar", "-C", str(root), "-xf", str(data)], check=True)
    return root

def soname_of(path):
    text = subprocess.run(["readelf", "-d", str(path)], check=True, capture_output=True, text=True).stdout
    for line in text.splitlines():
        if "SONAME" in line and "[" in line:
            return line.split("[", 1)[1].rstrip("]")
    raise RuntimeError(f"no SONAME in {path}")

def find_so(root, prefix):
    matches = [path for path in root.rglob(prefix) if path.is_file() and not path.is_symlink()]
    if not matches:
        matches = [path for path in root.rglob(prefix) if path.is_file()]
    if not matches:
        raise RuntimeError(f"no {prefix} under {root}")
    return matches[0]

def ar_member(name, data):
    if len(name) > 16:
        raise RuntimeError(name)
    header = f"{name:<16}{int(time.time()):<12}0     0     100644  {len(data):<10}`\n".encode()
    if len(header) != 60:
        raise RuntimeError(len(header))
    pad = b"\n" if len(data) % 2 else b""
    return header + data + pad

def make_deb(dest, package, depends, files):
    control = (
        f"Package: {package}\nVersion: 1.0\nArchitecture: amd64\n"
        f"Maintainer: Unifier Test <test@example.invalid>\nDepends: {depends}\n"
        f"Description: round fixture {package}\n"
    ).encode()
    sums = "".join(
        f"{hashlib.md5(data).hexdigest()}  {name}\n" for name, data, _mode in files
    ).encode()
    def tar_bytes(members):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w:xz") as archive:
            for name, data, mode in members:
                info = tarfile.TarInfo(name)
                info.mode = mode
                info.mtime = 1700000000
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                if name.endswith("/"):
                    info.type = tarfile.DIRTYPE
                    info.size = 0
                    archive.addfile(info)
                else:
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
        return raw.getvalue()
    control_tar = tar_bytes([
        ("./control", control, 0o644),
        ("./md5sums", sums, 0o644),
    ])
    directories = []
    seen = set()
    for name, _data, _mode in files:
        parent = Path(name).parent
        parts = parent.parts
        for index in range(1, len(parts) + 1):
            directory = "/".join(parts[:index]) + "/"
            if directory not in seen:
                seen.add(directory)
                directories.append((f"./{directory}", b"", 0o755))
    data_tar = tar_bytes(directories + [(f"./{name}", data, mode) for name, data, mode in files])
    blob = b"!<arch>\n" + ar_member("debian-binary", b"2.0\n") + ar_member("control.tar.xz", control_tar) + ar_member("data.tar.xz", data_tar)
    dest.write_bytes(blob)

def compile_maps(path, so, needle):
    source = CACHE / (path.name + ".c")
    source.write_text(
        '#include <stdio.h>\n#include <string.h>\nint main(void){\n'
        '  char line[512]; FILE *maps=fopen("/proc/self/maps","r"); if(!maps) return 2;\n'
        '  puts("ok");\n'
        f'  while(fgets(line,sizeof line,maps)) if(strstr(line,"{needle}")) fputs(line,stdout);\n'
        '  return 0;}\n')
    subprocess.run(["gcc", "-O2", "-o", str(path), str(source), "-Wl,--no-as-needed", str(so), "-Wl,--as-needed"], check=True)

def main():
    libs = {}
    for name, filename in FILES.items():
        deb = fetch(name, filename)
        root = extract(deb, CACHE / name)
        if name == "libconfig-dev":
            control = (CACHE / name / "control.tar.xz")
            text = subprocess.run(["tar", "-xO", "-f", str(next(CACHE.joinpath(name).glob("control.tar.*"))), "./control"], check=True, capture_output=True, text=True).stdout
            libs["libconfig_control"] = text
            continue
        so = find_so(root, "*.so*")
        libs[name] = {"soname": soname_of(so), "path": str(so)}
    print(json.dumps({key: value if isinstance(value, str) else value.get("soname") for key, value in libs.items()}, indent=2))
    estr = libs["libestr0"]
    fast = libs["libfastjson4"]
    cap = libs["libcapstone5"]
    bins = CACHE / "bin"
    bins.mkdir(exist_ok=True)
    compile_maps(bins / "unifier-round1107a", estr["path"], estr["soname"])
    compile_maps(bins / "unifier-round1107e", estr["path"], estr["soname"])
    compile_maps(bins / "unifier-round1107b", fast["path"], fast["soname"])
    plugin_c = CACHE / "plugin.c"
    plugin_c.write_text("int plugin_mark(void){return 1107;}\n")
    plugin = bins / "plugin.so"
    subprocess.run(["gcc", "-shared", "-fPIC", "-o", str(plugin), str(plugin_c), "-Wl,--no-as-needed", cap["path"], "-Wl,--as-needed"], check=True)
    main_c = CACHE / "main.c"
    main_c.write_text(
        '#include <dlfcn.h>\n#include <stdio.h>\nint main(void){\n'
        '  void *h=dlopen("/usr/lib/unifier-round1107c/plugin.so", RTLD_NOW);\n'
        '  if(!h){fprintf(stderr,"%s\\n",dlerror());return 1;}\n'
        '  int (*fn)(void)=dlsym(h,"plugin_mark"); if(!fn) return 2;\n'
        '  printf("ok %d\\n", fn()); return 0;}\n')
    subprocess.run(["gcc", "-O2", "-o", str(bins / "unifier-round1107c"), str(main_c), "-ldl"], check=True)
    dl_c = CACHE / "dlopen.c"
    dl_c.write_text(
        '#define _GNU_SOURCE\n#include <dlfcn.h>\n#include <link.h>\n#include <stdio.h>\nint main(void){\n'
        '  char name[64]; snprintf(name,sizeof name,"%s%s","lib","capstone.so.5");\n'
        '  void *h=dlopen(name, RTLD_NOW); if(!h){fprintf(stderr,"%s\\n",dlerror());return 1;}\n'
        '  struct link_map *map=0; if(dlinfo(h, RTLD_DI_LINKMAP, &map)||!map) return 2;\n'
        '  printf("ok %s\\n", map->l_name); return 0;}\n')
    subprocess.run(["gcc", "-O2", "-o", str(bins / "unifier-round1107h"), str(dl_c), "-ldl"], check=True)
    tiny_c = CACHE / "tiny.c"
    tiny_c.write_text("int main(void){return 0;}\n")
    for name in ("unifier-round1107p", "unifier-round1107q"):
        subprocess.run(["gcc", "-O2", "-o", str(bins / name), str(tiny_c)], check=True)
    def pack(package, depends, members):
        files = []
        for arc, src, mode in members:
            files.append((arc, Path(src).read_bytes(), mode))
        make_deb(OUT / f"{package}.deb", package, depends, files)
    pack("unifier-round1107a", "libestr0", [("usr/bin/unifier-round1107a", bins / "unifier-round1107a", 0o755)])
    pack("unifier-round1107e", "libestr0", [("usr/bin/unifier-round1107e", bins / "unifier-round1107e", 0o755)])
    pack("unifier-round1107b", "libc6", [("usr/bin/unifier-round1107b", bins / "unifier-round1107b", 0o755)])
    pack("unifier-round1107c", "libc6", [
        ("usr/bin/unifier-round1107c", bins / "unifier-round1107c", 0o755),
        ("usr/lib/unifier-round1107c/plugin.so", plugin, 0o644),
    ])
    pack("unifier-round1107h", "libc6", [("usr/bin/unifier-round1107h", bins / "unifier-round1107h", 0o755)])
    pack("unifier-round1107p", "libc6", [("usr/bin/unifier-round1107p", bins / "unifier-round1107p", 0o755)])
    pack("unifier-round1107q", "libc6", [("usr/bin/unifier-round1107q", bins / "unifier-round1107q", 0o755)])
    (ROOT / "fixtures.json").write_text(json.dumps({
        "libestr_soname": estr["soname"], "libfastjson_soname": fast["soname"],
        "libcapstone_soname": cap["soname"], "libconfig_control": libs.get("libconfig_control", ""),
    }, indent=2))
    for deb in sorted(OUT.glob("*.deb")):
        print(deb.name, deb.stat().st_size)

if __name__ == "__main__":
    main()
