"""Bounded, repository-backed repair of undeclared ELF dependencies.

Inspect data with readelf, never execute a vendor binary/ldd as root. This
checks installed package ELF objects (including dlopen-loaded plugins), not
arbitrary library names constructed at runtime, ABI versions or GUI behavior.
"""
from __future__ import annotations

from pathlib import PurePosixPath
import re

from unifier.errors import UnifierError
from unifier.packages import driver_for, parse_package_files, validate_package_name


SCAN = r'''
set -eu
command -v readelf >/dev/null
for file do
    [ -f "$file" ] || continue
    magic=$(head -c 4 -- "$file")
    [ "$magic" = "$(printf '\177ELF')" ] || continue
    printf 'UNIFIER_ELF\t%s\n' "$file"
    readelf -W -h -d -- "$file"
done
printf 'UNIFIER_SCAN_DONE\n'
'''

CACHE = r'''
set -eu
cache=$(/sbin/ldconfig -p)
printf '%s\n' "$cache" | while IFS= read -r line; do
    case "$line" in
        *'x86-64'*' => '*) path=${line##* => }; [ -r "$path" ] && printf '%s\n' "$line" ;;
    esac
    :
done
'''

LIBRARY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,160}\.so(?:\.[A-Za-z0-9_.+-]+)?\Z")


def validate_library(name: str) -> str:
    if not isinstance(name, str) or not LIBRARY.fullmatch(name):
        raise UnifierError(f"unsupported shared library name: {name!r}")
    return name


def provider_command(target: str, library: str) -> list[str]:
    library = validate_library(library)
    pattern = re.escape(library)
    if target == "debian13":
        return ["apt-file", "--architecture", "amd64", "--package-only", "--regexp",
                "search", r"^(usr/)?lib/(x86_64-linux-gnu/)?" + pattern + "$" ]
    if target == "fedora44":
        return ["dnf", "--quiet", "repoquery", "--available", "--arch=x86_64,noarch",
                "--whatprovides", library + "()(64bit)", "--queryformat", "%{name}"]
    if target == "arch":
        return ["pacman", "-F", "--machinereadable", "--regex", "^usr/lib/" + pattern + "$" ]
    raise UnifierError(f"no dependency provider adapter for {target}")


def provider_names(target: str, output: str) -> list[str]:
    names = set()
    for line in output.splitlines():
        if not line.strip():
            continue
        if target == "arch":
            fields = line.split("\0")
            if len(fields) != 4 or fields[0] not in {"core", "extra", "multilib"}:
                raise UnifierError("unexpected pacman file-provider record")
            name = fields[1]
        else:
            name = line.strip()
        names.add(validate_package_name(name))
    return sorted(names)


def parse_scan(output: str) -> list[dict]:
    lines = output.removesuffix("\n").split("\n")
    if lines[-1] != "UNIFIER_SCAN_DONE" or lines.count("UNIFIER_SCAN_DONE") != 1:
        raise UnifierError("ELF inspection did not complete")
    prefix, *blocks = re.split(r"(?m)^UNIFIER_ELF\t", "\n".join(lines[:-1]))
    if prefix.strip():
        raise UnifierError("unexpected ELF inspection output")
    objects = []
    for block in blocks:
        path, _, body = block.partition("\n")
        if not path.startswith("/") or any(ord(c) < 32 or ord(c) == 127 for c in path):
            raise UnifierError("ambiguous ELF inspection path")
        if not re.search(r"(?m)^\s*Class:\s+ELF64\s*$", body) or not re.search(
                r"(?m)^\s*Machine:\s+Advanced Micro Devices X86-64\s*$", body):
            raise UnifierError(f"dependency inspection supports x86_64 ELF64 only: {path}")
        needed = re.findall(r"\(NEEDED\).*?Shared library: \[([^\]]+)\]", body)
        sonames = re.findall(r"\(SONAME\).*?Library soname: \[([^\]]+)\]", body)
        objects.append({"path": path, "needed": [validate_library(n) for n in needed],
                        "sonames": [validate_library(n) for n in sonames]})
    return objects


class DependencyRepair:
    def __init__(self, service, record: dict, transaction: dict):
        self.service, self.record, self.txn = service, record, transaction
        self.driver = driver_for(record)
        self.target = record["target"]
        self.readelf_ready = False
        self.report = {"status": "checking", "scope": "package-elf-needed",
                       "objects": 0, "rounds": [], "unresolved": [],
                       "bundled_candidates": [], "installed": [],
                       "runtime_loading": "unverified"}
        transaction["dependency_repair"] = self.report

    def capture(self, argv: list[str], *, accepted=(0,), timeout=120):
        result = self.service._exec_record(self.record, argv, timeout=timeout, native=True)
        if result.returncode not in accepted:
            raise UnifierError("dependency query failed: " + " ".join(argv[:3]) + ": "
                               + (result.stderr or result.stdout)[-1500:])
        return result

    def change(self, steps):
        self.service._run_native(self.record, self.txn, steps)
        if self.txn["failure_phase"] is not None:
            raise UnifierError("native dependency installation failed or was declined")

    def run(self, packages: list[str], libraries: list[str] = ()) -> dict:
        try:
            return self._run(packages, libraries)
        except Exception as exc:
            self.report["status"] = "unresolved"
            self.report["error"] = str(exc)
            raise

    def inspect_packages(self, packages: list[str]) -> list[dict]:
        files = set()
        for package in packages:
            package = validate_package_name(package)
            argv = {"debian13": ["dpkg-query", "-L", package],
                    "fedora44": ["rpm", "-ql", package],
                    "arch": ["pacman", "-Ql", package]}[self.target]
            files.update(parse_package_files(package, self.capture(argv).stdout))
        # Package file lists are data, passed as literal arguments in chunks.
        paths = sorted(files)
        if any(any(ord(c) < 32 or ord(c) == 127 for c in path) for path in paths):
            raise UnifierError("ambiguous package file list")
        objects = []
        if paths and not self.readelf_ready:
            probe = self.capture(["/bin/sh", "-c", "command -v readelf"], accepted=(0, 1, 127))
            if probe.returncode != 0:
                self.change(self.driver.install(["binutils"]))
            self.readelf_ready = True
        if paths:
            for start in range(0, len(paths), 128):
                chunk = paths[start:start + 128]
                output = self.capture(["/bin/sh", "-c", SCAN, "unifier-elf", *chunk],
                                      timeout=180).stdout
                inspected = parse_scan(output)
                if any(obj["path"] not in chunk for obj in inspected):
                    raise UnifierError("ELF inspection returned an unrequested path")
                objects.extend(inspected)
        return objects

    def _run(self, packages: list[str], libraries) -> dict:
        hints = {validate_library(name) for name in libraries}
        packages = {validate_package_name(package) for package in packages}
        # A repository provider must appear in the loader cache. Once repair
        # selects it, its newly scanned SONAME cannot count as a bundled copy
        # and mask an ineffective installation.
        repository_required = set(hints)
        provider_ready = False
        for attempt in range(3):
            # Native installation may upgrade original files, and providers
            # can themselves have undeclared dependencies. Re-read both.
            scope = sorted(packages | set(self.report["installed"]))
            objects = self.inspect_packages(scope)
            self.report["objects"] = len(objects)
            self.report["packages"] = scope
            required = hints | {name for obj in objects for name in obj["needed"]}
            bundled = {name for obj in objects for name in obj["sonames"]}
            bundled.update(PurePosixPath(obj["path"]).name for obj in objects)
            available = set()
            if required:
                cache = self.capture(["/bin/sh", "-c", CACHE, "unifier-cache"]).stdout
                available = {line.split()[0] for line in cache.splitlines() if " => " in line}
            self.report["bundled_candidates"] = sorted((required - available) & bundled - repository_required)
            missing = sorted(required - available - (bundled - repository_required))
            self.report["unresolved"] = missing
            if not missing:
                self.report["status"] = "repaired" if self.report["installed"] else "no-missing-elf-dependencies"
                return self.report
            if attempt == 2:
                raise UnifierError("dependency repair did not converge: " + ", ".join(missing))
            repository_required.update(missing)
            if not provider_ready:
                if self.target == "debian13":
                    probe = self.capture(["/bin/sh", "-c", "command -v apt-file"], accepted=(0, 1, 127))
                    if probe.returncode:
                        self.change(self.driver.install(["apt-file"]))
                    self.change([(["apt-file", "update"], 900)])
                elif self.target == "arch":
                    self.change([(["pacman", "-Fy"], 900)])
                provider_ready = True
            resolution = {"missing": missing, "providers": {}, "selected": [], "queries": {}}
            self.report["rounds"].append(resolution)
            for library in missing:
                result = self.capture(provider_command(self.target, library), accepted=(0, 1), timeout=180)
                resolution["queries"][library] = {"exit_code": result.returncode,
                                                   "stderr_tail": result.stderr[-1500:]}
                if result.returncode and result.stdout.strip():
                    raise UnifierError("dependency provider query failed with partial output: "
                                       + library + ": " + result.stderr[-1500:])
                candidates = provider_names(self.target, result.stdout)
                resolution["providers"][library] = candidates
                if len(candidates) != 1:
                    raise UnifierError(f"dependency {library} has {len(candidates)} repository providers; "
                                       "automatic selection is unresolved")
                resolution["selected"].extend(candidates)
            selected = sorted(set(resolution["selected"]))
            if set(selected) <= set(self.report["installed"]):
                raise UnifierError("installed provider did not satisfy the missing library")
            self.service.log("automatic dependency repair: " + ", ".join(selected))
            self.change(self.driver.install(selected))
            self.report["installed"].extend(selected)
        raise AssertionError("bounded dependency repair")
