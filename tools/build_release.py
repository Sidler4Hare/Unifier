#!/usr/bin/env python3
"""Build a versioned source bundle and check the independently extracted copy."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from unifier import __version__


SOURCE_DIRS = ("src", "tools", "tests", "config", "fixtures", "bin", "docs", "设计")
SOURCE_FILES = ("README.md", "README.zh-CN.md", "HANDOFF.md", "AGENTS.md", ".gitignore", ".gitattributes")
WPS_DESTINATION = "src/unifier/wps_portal_assets"
WPS_SOURCE_VERSION = "0.0.8b1+render.1.portal.2.install.1.media.1.desktop.1"
WPS_SOURCE_MANIFEST_SHA256 = "b66a51299aa3dbbf63e99a6f71148b928af4156f457aaf6745da74902507187c"
WPS_SOURCE_ARCHIVE_SHA256 = "75a91845fa8210f9200b0d54ff921517d38df3842e81d893eb909bd6cd66c435"
WPS_FILES = (
    "LICENSE.LGPL3", "manifest.json", "platformthemes/libqxdgdesktopportal.so",
    "wps-format-audit.so",
)
MAX_WPS_INPUT_BYTES = 1024 * 1024


def inventory_digest(files: dict) -> str:
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def source_provenance(files: dict, root: Path | None = None) -> dict:
    root = ROOT if root is None else root
    baseline = root / "config/release-baseline.json"
    base = None
    if baseline.exists():
        value = json.loads(baseline.read_text())
        base = {key: value[key] for key in ("version", "package_sha256", "manifest_sha256")}
        if not isinstance(base['version'], str) or not base['version']:
            raise ValueError('invalid release baseline version')
        for key in ('package_sha256', 'manifest_sha256'):
            digest = base[key]
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise ValueError('invalid release baseline digest')
    revision = None
    if (root / '.git').exists():
        try:
            revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root,
                                               text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return {'source_tree_sha256': inventory_digest(files),
            'source_revision': revision, 'base_release': base}


def _candidate_revision() -> str:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    checkout = subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], cwd=ROOT, env=env, text=True, timeout=10).strip()
    if Path(checkout).resolve() != ROOT.resolve():
        raise ValueError("candidate must be built from its source checkout root")
    revision = subprocess.check_output(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"], cwd=ROOT, env=env,
        text=True, timeout=10).strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
        raise ValueError("candidate requires a committed source revision")
    status = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=ROOT,
        env=env, timeout=10)
    if status:
        raise ValueError("candidate requires a clean source checkout")
    return revision


def _candidate_snapshot(root: Path, revision: str) -> None:
    root.mkdir()
    with tempfile.TemporaryFile() as source:
        subprocess.run(
            ["git", "-c", "tar.umask=0022", "archive", "--format=tar", revision,
             "--", *SOURCE_DIRS, *SOURCE_FILES],
            cwd=ROOT, stdout=source, check=True, timeout=30)
        source.seek(0)
        with tarfile.open(fileobj=source) as archive:
            archive.extractall(root, filter="data")


def _read_regular(path: str | Path, *, dir_fd: int | None = None) -> tuple[bytes, int]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=dir_fd)
    with os.fdopen(descriptor, "rb") as stream:
        initial = os.fstat(stream.fileno())
        if not stat.S_ISREG(initial.st_mode) or not 0 < initial.st_size <= MAX_WPS_INPUT_BYTES:
            raise ValueError(f"invalid WPS candidate input: {path}")
        content = stream.read(MAX_WPS_INPUT_BYTES + 1)
    if len(content) != initial.st_size:
        raise ValueError(f"WPS candidate input changed during read: {path}")
    return content, stat.S_IMODE(initial.st_mode)


def _candidate_assets(inputs: Path, manifest: Path) -> dict:
    for path in (inputs, manifest):
        if path.absolute() != path.resolve(strict=True):
            raise ValueError(f"WPS candidate source must not traverse a symlink: {path}")
    manifest_bytes, _mode = _read_regular(manifest)
    if hashlib.sha256(manifest_bytes).hexdigest() != WPS_SOURCE_MANIFEST_SHA256:
        raise ValueError("WPS source manifest differs from the frozen desktop release")
    source = json.loads(manifest_bytes)
    if source["version"] != WPS_SOURCE_VERSION:
        raise ValueError("unexpected WPS asset source release")
    declared = {path: record for path, record in source["files"].items()
                if path.startswith(WPS_DESTINATION + "/")}
    expected = {f"{WPS_DESTINATION}/{name}" for name in WPS_FILES}
    if declared.keys() != expected:
        raise ValueError("unexpected WPS asset inventory in source manifest")

    assets = {}
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    base = os.open(inputs, directory_flags)
    try:
        if set(os.listdir(base)) != {"LICENSE.LGPL3", "manifest.json", "platformthemes", "wps-format-audit.so"}:
            raise ValueError("unexpected WPS candidate input inventory")
        themes = os.open("platformthemes", directory_flags, dir_fd=base)
        try:
            if set(os.listdir(themes)) != {"libqxdgdesktopportal.so"}:
                raise ValueError("unexpected WPS candidate platformtheme inventory")
            for name in WPS_FILES:
                directory = themes if name.startswith("platformthemes/") else base
                content, mode = _read_regular(Path(name).name, dir_fd=directory)
                relative = f"{WPS_DESTINATION}/{name}"
                if declared[relative] != _file_record(content, mode):
                    raise ValueError(f"WPS asset differs from the frozen desktop release: {name}")
                assets[relative] = (content, mode)
        finally:
            os.close(themes)
    finally:
        os.close(base)
    return assets


def _file_record(content: bytes, mode: int) -> dict:
    return {"kind": "file", "sha256": hashlib.sha256(content).hexdigest(), "mode": mode}


def build(output: Path, *, candidate_version: str | None = None,
          candidate_inputs: Path | None = None, candidate_source_manifest: Path | None = None) -> dict:
    if (candidate_version, candidate_inputs, candidate_source_manifest) == (None, None, None):
        return _build(output, ROOT, __version__)
    if candidate_version is None or candidate_inputs is None or candidate_source_manifest is None:
        raise ValueError("candidate version, inputs and source manifest must be supplied together")
    suffix = r"(?:\+[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?"
    if not re.fullmatch(re.escape(__version__) + suffix, candidate_version):
        raise ValueError(f"candidate version must match the source version {__version__} with an optional suffix")
    revision = _candidate_revision()
    assets = _candidate_assets(candidate_inputs, candidate_source_manifest)
    with tempfile.TemporaryDirectory(prefix="unifier-release-source-") as temporary:
        root = Path(temporary) / "source"
        _candidate_snapshot(root, revision)
        return _build(output, root, candidate_version, revision=revision, assets=assets)


def _build(output: Path, root: Path, version: str, *, revision: str | None = None,
           assets: dict | None = None) -> dict:
    name = f"unifier-{version}"
    sidecars = [output, Path(str(output) + ".sha256"), Path(str(output) + ".manifest.json"),
                Path(str(output) + ".validation.log")]
    if any(p.exists() or p.is_symlink() for p in sidecars):
        raise FileExistsError("refusing to overwrite release files; choose a new output path")
    paths = []
    for directory in SOURCE_DIRS:
        paths.extend(p for p in (root / directory).rglob("*")
                     if (p.is_file() or p.is_symlink()) and "__pycache__" not in p.parts
                     and p.suffix != ".pyc")
    paths.extend(root / p for p in SOURCE_FILES)
    manifest = {"schema_version": 1, "version": version,
                "source_kind": "git-candidate" if revision else "working-tree", "files": {}}
    snapshots = {}
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode & 0o777
        if path.is_symlink():
            if not path.resolve().is_relative_to(root):
                raise ValueError(f"external source link: {relative}")
            manifest["files"][relative] = {"kind": "symlink", "target": os.readlink(path), "mode": mode}
        else:
            snapshots[relative] = path.read_bytes()
            manifest["files"][relative] = _file_record(snapshots[relative], mode)
    candidate = {}
    if revision is not None:
        files = manifest["files"]
        candidate["source_snapshot_sha256"] = inventory_digest(files)
        version_file = "src/unifier/__init__.py"
        original = f'__version__ = "{__version__}"'.encode()
        replacement = f'__version__ = "{version}"'.encode()
        if version_file not in snapshots or snapshots[version_file].count(original) != 1:
            raise ValueError("source snapshot version does not match the builder")
        snapshots[version_file] = snapshots[version_file].replace(original, replacement)
        files[version_file] = _file_record(snapshots[version_file], files[version_file]["mode"])
        if os.path.lexists(root / WPS_DESTINATION):
            raise ValueError("candidate source already contains the WPS asset directory")
        for relative, (content, mode) in assets.items():
            snapshots[relative] = content
            files[relative] = _file_record(content, mode)
        candidate["wps_asset_source"] = {
            "version": WPS_SOURCE_VERSION,
            "archive_sha256": WPS_SOURCE_ARCHIVE_SHA256,
            "manifest_sha256": WPS_SOURCE_MANIFEST_SHA256,
            "files": {relative: files[relative] for relative in assets},
        }
    provenance = source_provenance(manifest['files'], root)
    if revision is not None:
        provenance.update(candidate, source_revision=revision)
    manifest.update(provenance)
    payload = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="unifier-release-check-") as temporary:
        work = Path(temporary)
        candidate = work / "candidate.tar.gz"
        with candidate.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w") as archive:
                for relative, record in manifest["files"].items():
                    info = tarfile.TarInfo(f"{name}/{relative}")
                    info.mode = record['mode']
                    if record["kind"] == "symlink":
                        info.type = tarfile.SYMTYPE
                        info.linkname = record["target"]
                        archive.addfile(info)
                    else:
                        content = snapshots[relative]
                        info.size = len(content)
                        archive.addfile(info, io.BytesIO(content))
                info = tarfile.TarInfo(f"{name}/BUNDLE-MANIFEST.json")
                info.mode, info.size = 0o644, len(payload)
                archive.addfile(info, io.BytesIO(payload))
        with tarfile.open(candidate) as archive:
            archive.extractall(work, filter="data")
        extracted = work / name
        if revision is not None:
            if (extracted / "BUNDLE-MANIFEST.json").read_bytes() != payload:
                raise ValueError("extracted candidate manifest differs")
            for relative, (expected, mode) in assets.items():
                if _read_regular(extracted / relative) != (expected, mode):
                    raise ValueError(f"extracted WPS asset differs: {relative}")
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                       "LC_ALL": "C", "LANG": "C", "PYTHONPATH": str(extracted / "src")}
        with sidecars[3].open("w") as log:
            completed = subprocess.run([sys.executable, "tools/check_repo.py"], cwd=extracted,
                                       env=environment, stdout=log, stderr=subprocess.STDOUT)
        completed.check_returncode()
        output.write_bytes(candidate.read_bytes())
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    sidecars[1].write_text(f"{digest}  {output.name}\n")
    sidecars[2].write_bytes(payload)
    return {"version": version, "path": str(output), "sha256": digest,
            "source_files": len(manifest["files"]), "independent_extracted_check": "passed", **provenance}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--candidate-version", help="version for a clean committed source candidate")
    parser.add_argument("--candidate-inputs", type=Path, help="directory with the four frozen WPS assets")
    parser.add_argument("--candidate-source-manifest", type=Path, help="frozen desktop release manifest")
    args = parser.parse_args()
    output = args.output or ROOT / "artifacts" / f"unifier-{args.candidate_version or __version__}.tar.gz"
    try:
        print(json.dumps(build(
            output.absolute(), candidate_version=args.candidate_version,
            candidate_inputs=args.candidate_inputs,
            candidate_source_manifest=args.candidate_source_manifest), ensure_ascii=False, indent=2))
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print(f"release build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
