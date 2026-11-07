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
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from unifier import __version__


def build(output: Path) -> dict:
    name = f"unifier-{__version__}"
    sidecars = [output, Path(str(output) + ".sha256"), Path(str(output) + ".manifest.json"),
                Path(str(output) + ".validation.log")]
    if any(p.exists() for p in sidecars):
        raise FileExistsError("refusing to overwrite release files; choose a new output path")
    output.parent.mkdir(parents=True, exist_ok=True)
    paths = []
    for directory in ("src", "tools", "tests", "config", "fixtures", "bin", "docs", "设计"):
        paths.extend(p for p in (ROOT / directory).rglob("*")
                     if (p.is_file() or p.is_symlink()) and "__pycache__" not in p.parts
                     and p.suffix != ".pyc")
    paths.extend(ROOT / p for p in ("README.md", "HANDOFF.md", "AGENTS.md", ".gitignore", ".gitattributes"))
    manifest = {"schema_version": 1, "version": __version__, "source_kind": "working-tree", "files": {}}
    snapshots = {}
    for path in sorted(paths):
        relative = path.relative_to(ROOT).as_posix()
        if path.is_symlink():
            if not path.resolve().is_relative_to(ROOT):
                raise ValueError(f"external source link: {relative}")
            manifest["files"][relative] = {"kind": "symlink", "target": os.readlink(path)}
        else:
            snapshots[relative] = path.read_bytes()
            manifest["files"][relative] = {"kind": "file", "sha256": hashlib.sha256(snapshots[relative]).hexdigest()}
    payload = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    with tempfile.TemporaryDirectory(prefix="unifier-release-check-") as temporary:
        work = Path(temporary)
        candidate = work / "candidate.tar.gz"
        with candidate.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w") as archive:
                for relative, record in manifest["files"].items():
                    info = tarfile.TarInfo(f"{name}/{relative}")
                    info.mode = (ROOT / relative).lstat().st_mode & 0o777
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
    return {"version": __version__, "path": str(output), "sha256": digest,
            "source_files": len(manifest["files"]), "independent_extracted_check": "passed"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / f"unifier-{__version__}.tar.gz")
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.output.resolve()), ensure_ascii=False, indent=2))
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"release build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
