#!/usr/bin/env python3
"""Compare file-mapping PSS for hard links, ordinary copies, and reflinks.

Creates bounded temporary files under the workspace and two short-lived child
processes per case. Reads only those children's /proc mappings. Reports mapping
memory, not whole-process/container memory or application performance.
"""

import argparse
import datetime as dt
import hashlib
import json
import mmap
import os
from pathlib import Path
import platform
import re
import select
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo


FIELDS = {"Rss", "Pss", "Shared_Clean", "Shared_Dirty", "Private_Clean",
          "Private_Dirty", "Anonymous", "Swap"}


def worker(path):
    with open(path, "rb") as stream, mmap.mmap(
        stream.fileno(), 0, access=mmap.ACCESS_READ
    ) as mapping:
        checksum = sum(mapping[i] for i in range(0, len(mapping), mmap.PAGESIZE))
        print(json.dumps({"ready": True, "page_sample_checksum": checksum}), flush=True)
        sys.stdin.readline()


def mapping_stats(pid, path):
    result = {name + "_bytes": 0 for name in FIELDS}
    active = False
    matched = False
    for line in Path(f"/proc/{pid}/smaps").read_text().splitlines():
        if re.match(r"^[0-9a-f]+-[0-9a-f]+\s", line):
            active = line.endswith(" " + str(path))
            matched = matched or active
        elif active:
            key, _, value = line.partition(":")
            if key in FIELDS:
                result[key + "_bytes"] += int(value.split()[0]) * 1024
    if not matched:
        raise RuntimeError("Expected file mapping not found in child smaps")
    return result


def measure_pair(paths):
    children = []
    try:
        ready = []
        for path in paths:
            child = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--worker", str(path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            )
            children.append(child)
            readable, _, _ = select.select([child.stdout], [], [], 10)
            if not readable:
                raise RuntimeError("Child did not become ready within 10 seconds")
            line = child.stdout.readline()
            if not line:
                raise RuntimeError("Child exited before reporting its mapping")
            ready.append(json.loads(line))
        stats = [mapping_stats(child.pid, path) for child, path in zip(children, paths)]
        return {
            "children_ready": ready,
            "mapping_stats": stats,
            "sum_mapping_pss_bytes": sum(item["Pss_bytes"] for item in stats),
            "sum_mapping_rss_bytes": sum(item["Rss_bytes"] for item in stats),
        }
    finally:
        for child in children:
            if child.poll() is None:
                try:
                    child.communicate("\n", timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate()
            else:
                child.communicate()


def probe(workspace, size):
    result = {
        "schema_version": 1,
        "captured_at": dt.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "kernel_release": platform.release(), "file_size_bytes": size,
        "page_size_bytes": mmap.PAGESIZE, "cases": [],
        "limitations": [
            "One controlled run per case; not a timing benchmark or a distro compatibility test.",
            "PSS covers only the explicitly mapped test file, excluding Python heaps and kernel metadata.",
            "Extent-sharing figures are not a full filesystem physical-space measurement.",
            "Temporary files and child processes are removed after measurement.",
        ],
    }
    with tempfile.TemporaryDirectory(prefix=".file-sharing-probe-", dir=workspace) as scratch:
        for mode in ("hardlink", "ordinary_copy", "reflink"):
            directory = Path(scratch) / mode
            directory.mkdir()
            source, target = directory / "source.bin", directory / "target.bin"
            with source.open("wb") as stream:
                stream.write(os.urandom(size))
                stream.flush()
                os.fsync(stream.fileno())
            if mode == "hardlink":
                os.link(source, target)
            else:
                option = "--reflink=always" if mode == "reflink" else "--reflink=never"
                subprocess.run(["cp", option, str(source), str(target)], check=True,
                               capture_output=True, text=True, timeout=10)
                with target.open("rb") as stream:
                    os.fsync(stream.fileno())
            hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, target)]
            if hashes[0] != hashes[1]:
                raise RuntimeError("Copy contents differ")
            extent = subprocess.run(
                ["btrfs", "filesystem", "du", "-s", "--raw", str(source), str(target)],
                capture_output=True, text=True, timeout=10,
            )
            a, b = source.stat(), target.stat()
            case = {
                "mode": mode, "contents_equal": True, "sha256": hashes[0],
                "same_device_and_inode": (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino),
                "extent_report_returncode": extent.returncode,
                "extent_report": extent.stdout.replace(scratch, "<scratch>"),
            }
            case.update(measure_pair([source, target]))
            result["cases"].append(case)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--size-mib", type=int, default=8, choices=range(1, 33), metavar="1..32")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker)
        return
    result = probe(args.workspace.resolve(), args.size_mib * 1024 * 1024)
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
