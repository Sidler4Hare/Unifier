"""Real SIGKILL at durable desktop revoke intent and each undo boundary."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import traceback
import uuid

from probe_native_desktop import CONTROLLER

HERE = Path(__file__).resolve().parent


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--desktop-proof", type=Path, required=True)
    p.add_argument("--workdir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    assert not a.output.exists() and not a.workdir.exists()
    proof = json.loads(a.desktop_proof.read_text())
    assert proof["passed"]
    fixtures = json.loads((HERE / "fixtures.json").read_text())
    project = Path(fixtures["project"])
    report = {"passed": False, "source_sha256": proof["source_sha256"], "cases": {},
              "scope": "actual SIGKILL + fsync journal/file effects in isolated XDG data, not power-loss testing"}
    a.workdir.mkdir(mode=0o700)

    def invoke(base, action, **params):
        child = subprocess.run([sys.executable, "-c", CONTROLLER, str(project), str(base)],
            input=json.dumps(dict(action=action, **params)), text=True, capture_output=True, timeout=20,
            env=os.environ)
        if action == "revoke-crash":
            assert child.returncode == -signal.SIGKILL, (child.returncode, child.stderr[-2000:])
            return None
        assert child.returncode == 0, (action, child.stderr[-2500:])
        return json.loads(child.stdout)

    try:
        for count in range(4):
            base = (a.workdir / str(count)).resolve()
            apps, packages = base / "data/applications", base / "data/mime/packages"
            apps.mkdir(parents=True)
            packages.mkdir(parents=True)
            nonce = uuid.uuid4().hex[:8]
            mime, extension = "application/x-multilinux-ewin-" + nonce, "w" + nonce
            original = ("# original user bytes\n[Default Applications]\n" + mime + "=original-window.desktop;\n").encode()
            mimeapps = apps / "mimeapps.list"
            mimeapps.write_bytes(original)
            (apps / "original-window.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Original window probe\nExec=/usr/bin/true\nMimeType=" + mime + ";\n")
            record = invoke(base, "install", project_identifier="native-debian13", app_id=proof["app_id"],
                            mime_type=mime, extension=extension, desktop_alias="ewin-" + nonce)
            assert record["state"] == "installed"
            invoke(base, "revoke-crash", identifier=record["integration_uuid"], after_files=count)
            interrupted = invoke(base, "show", identifier=record["integration_uuid"])
            assert interrupted["state"] == "revoking", interrupted["state"]
            invoke(base, "recover")
            invoke(base, "recover")
            current = invoke(base, "show", identifier=record["integration_uuid"])
            assert current["state"] == "revoked", current
            assert mimeapps.read_bytes() == original
            assert not Path(record["desktop_path"]).exists()
            assert (apps / "original-window.desktop").exists()
            assert not (packages / (record["integration_uuid"] + ".xml")).exists()
            report["cases"]["SIGKILL-after-" + str(count) + "-files"] = {
                "passed": True, "persisted_revoke_intent": True, "worker_signal": "SIGKILL",
                "recover_twice": True, "original_bytes_restored": True, "entry_never_regranted": True}
        assert all(hashlib.sha256((project / name).read_bytes()).hexdigest() == value
                   for name, value in report["source_sha256"].items())
        report["passed"] = True
    except Exception:
        report["error"] = traceback.format_exc()[-8000:]
    with a.output.open("x") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"passed": report["passed"], "cases": len(report["cases"]), "output": str(a.output)}))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
