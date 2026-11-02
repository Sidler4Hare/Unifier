#!/usr/bin/env python3
"""Independent boundary probes for the CLI guest directory resolver."""
import argparse
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("refusing to overwrite a previous probe")
    from multilinux.guest_paths import list_guest_directories
    checks = {}
    errors = {}
    with tempfile.TemporaryDirectory(prefix="unifier-path-review-") as temporary:
        root = Path(temporary)
        (root / "a").mkdir()
        (root / "b").mkdir()
        (root / "b/real-subdirectory").mkdir()
        (root / "a/relative-link").symlink_to("../b")
        (root / "absolute-link").symlink_to("/b")
        # The current FUSE view rewrites symlinks to host-visible mount paths.
        # This is a structural fixture, not real-FUSE acceptance.
        (root / "projected-link").symlink_to(str(root / "b"))
        cases = [
            ("relative_link_listed", "/a", ["relative-link"]),
            ("relative_link_followed", "/a/relative-link", ["real-subdirectory"]),
            ("absolute_guest_link", "/absolute-link", ["real-subdirectory"]),
            ("projected_mount_link", "/projected-link", ["real-subdirectory"]),
        ]
        for name, path, expected in cases:
            try:
                result = list_guest_directories(
                    root, path, projected=name == "projected_mount_link")
                checks[name] = result["entries"] == expected
                if not checks[name]:
                    errors[name] = {"expected": expected, "observed": result}
            except Exception as exc:
                checks[name] = False
                errors[name] = {"error": type(exc).__name__ + ": " + str(exc)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "passed": all(checks.values()), "checks": checks, "errors": errors,
        "scope": "local resolver fixtures; projected-link case requires explicit view semantics",
    }, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"checks": checks, "passed": all(checks.values())}))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
