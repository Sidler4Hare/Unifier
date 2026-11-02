"""Validate a legacy deployment's policy and creation gate without container writes."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

root = Path(sys.argv[1]).resolve()
sys.path[:0] = [str(root / "src"), str(root)]
from multilinux import cli, compatibility
from multilinux.errors import MultilinuxError
from multilinux.project import load_project
from multilinux.service import EnvironmentService

config, lock, _ = load_project()


class NoRuntime:
    def __getattr__(self, name):
        raise AssertionError(f"runtime reached: {name}")


def host(distro):
    return {"os_release": {"ID": distro}, "system": "Linux", "machine": "x86_64",
            "libc": "glibc", "ostree": False, "transactional": False}


checks = []
for distro, target in (("ubuntu", "debian13"), ("fedora", "fedora44"), ("arch", "arch")):
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        service = EnvironmentService(work, NoRuntime(), config, lock, host_facts=host(distro))
        try:
            service.create(target, None, [])
        except MultilinuxError as exc:
            assert "same-family containers are forbidden" in str(exc)
        else:
            raise AssertionError("same-family creation admitted")
        assert list(work.iterdir()) == []
        checks.append({"host": distro, "target": target, "refused_without_runtime_or_state": True})

with tempfile.TemporaryDirectory() as temporary:
    service = EnvironmentService(Path(temporary), NoRuntime(), config, lock, host_facts=host("rhel"))
    service.store.save({"id": "legacy", "target": "debian13"}, None)
    try:
        service.create("debian13", "other", [])
    except MultilinuxError as exc:
        assert "only one environment per target" in str(exc)
    else:
        raise AssertionError("duplicate target admitted")

with patch.object(compatibility, "observe_host", return_value=host("ubuntu")), \
     contextlib.redirect_stdout(io.StringIO()) as output:
    assert cli.main(["compat", "matrix"]) == 0
    assert any(row["allowed"] for row in json.loads(output.getvalue()))

print(json.dumps({"root": str(root), "policy_version": compatibility.POLICY_VERSION,
                  "negative_checks": checks, "duplicate_refused": True,
                  "mixed_matrix_succeeds": True}, ensure_ascii=False, indent=2))
