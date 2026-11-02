"""Build and independently validate a policy-v2 portable source snapshot."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
NAME = "unifier-source-20261030-policy2"
archive_path = ROOT / "artifacts" / f"{NAME}.tar.gz"
if archive_path.exists():
    raise SystemExit("refusing to overwrite an existing portable bundle")
paths = []
for name in ("src", "tools", "tests", "config", "fixtures", "bin", "docs", "设计"):
    paths.extend(path for path in (ROOT / name).rglob("*")
                 if (path.is_file() or path.is_symlink())
                 and "__pycache__" not in path.parts and path.suffix != ".pyc")
paths.extend(ROOT / name for name in ("README.md", "HANDOFF.md", ".gitignore", ".gitattributes"))
manifest = {"source_kind": "working-tree", "policy_version": 2, "files": {}}
for path in sorted(paths):
    relative = path.relative_to(ROOT).as_posix()
    if path.is_symlink():
        assert path.resolve().is_relative_to(ROOT)
        manifest["files"][relative] = {"kind": "symlink", "target": os.readlink(path)}
    else:
        manifest["files"][relative] = {"kind": "file", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
payload = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
with tempfile.TemporaryDirectory(prefix="unifier-portable-check-") as temporary:
    work = Path(temporary)
    candidate = work / archive_path.name
    with tarfile.open(candidate, "w:gz") as archive:
        for path in sorted(paths):
            archive.add(path, arcname=f"{NAME}/{path.relative_to(ROOT).as_posix()}", recursive=False)
        info = tarfile.TarInfo(f"{NAME}/BUNDLE-MANIFEST.json")
        info.size = len(payload)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(payload))
    with tarfile.open(candidate) as archive:
        archive.extractall(work, filter="data")
    extracted = work / NAME
    for relative, item in manifest["files"].items():
        path = extracted / relative
        if item["kind"] == "symlink":
            assert path.is_symlink() and os.readlink(path) == item["target"]
        else:
            assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C", "LANG": "C",
                   "PYTHONPATH": os.pathsep.join([str(extracted / "src"), str(extracted)])}
    with (OUT / "bundle-validation.log").open("w") as log:
        # check() validates a source tree without requiring a shipped Git index.
        command = [sys.executable, "-c", "from pathlib import Path; from tools.check_repo import check; check(Path.cwd(), quick=False)"]
        completed = subprocess.run(command, cwd=extracted, env=environment,
                                   stdout=log, stderr=subprocess.STDOUT)
    completed.check_returncode()
    archive_path.write_bytes(candidate.read_bytes())
digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
archive_path.with_name(archive_path.name + ".sha256").write_text(f"{digest}  {archive_path.name}\n")
(OUT / "bundle-manifest.json").write_bytes(payload)
receipt = {"path": str(archive_path), "sha256": digest, "source_files": len(paths),
           "policy_version": 2, "extracted_files_verified": True,
           "extracted_repository_check_passed": True,
           "scope": "Fedora-local extracted-source offline checks; no remote deployment"}
(OUT / "bundle.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(receipt, ensure_ascii=False, indent=2))
