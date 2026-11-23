"""Install only the authenticated Fedora 44 closure in the fixed AMD fixture."""
import hashlib
import os
from pathlib import Path
import re
import sys

from tools import reuse_test_guard as reuse
from tools.hardware_test_guard import (
    AMD_BOOTSTRAP_CID, AMD_DNF_BINDING, AMD_DNF_CACHE_ROOT, AMD_DNF_EXACT_PLAN,
    AMD_DNF_INSTALL_RETRY_PROFILE, AMD_DNF_SIGNING_FINGERPRINT, AMD_DNF_SUCCEEDED_QUERY,
    amd_install_submission_time, amd_offline_libraries, bound_bytes, bound_read, make_service,
    package_names,
)
from unifier import package_journal
from unifier.identity import binding_for
from unifier.packages import inventory_argv
from unifier.package_inventory import normalize_inventory

NATIVE_ROOT = "/var/cache/libdnf5"
MIB = 1024 ** 2
COPY = r'''
set -f
cache=$1
shift
checked_dir() {
    [ "$(realpath -- "$1")" = "$1" ] && [ ! -L "$1" ] && [ -d "$1" ] &&
        [ "$(stat -c %u -- "$1")" = 0 ]
}
checked_file() {
    [ "$(realpath -- "$1")" = "$1" ] && [ ! -L "$1" ] && [ -f "$1" ] &&
        [ "$(stat -c %u -- "$1")" = 0 ] && [ "$(stat -c %s -- "$1")" = "$2" ] &&
        [ "$(head -c "$(($2 + 1))" -- "$1" | sha256sum | cut -d ' ' -f1)" = "$3" ]
}
checked_dir "$cache"
for key in fedora-cff72538bc9825a4 updates-3cc07c89a20302f2; do
    checked_dir "$cache/$key"
    checked_dir "$cache/$key/packages"
done
while [ "$#" -gt 0 ]; do
    dst=$1 size=$2 sha=$3
    shift 3
    before=$(stat -c '%d:%i:%f:%u:%s:%y:%z' -- "$dst")
    checked_file "$dst" "$size" "$sha"
    [ "$(stat -c '%d:%i:%f:%u:%s:%y:%z' -- "$dst")" = "$before" ]
    printf '%s %s %s\n' "$sha" "$size" "$dst"
done
'''


def verify_signatures(output, destinations):
    blocks = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        if line.endswith(":") and line[:-1] in destinations:
            path = line[:-1]
            if path in blocks:
                raise RuntimeError("duplicate RPM signature output")
            blocks[path] = []
        elif not blocks:
            raise RuntimeError("unbound RPM signature output")
        else:
            blocks[path].append(line.strip())
    if list(blocks) != destinations:
        raise RuntimeError("missing or unordered RPM signature output")
    for details in blocks.values():
        signatures = [row for row in details if "signature" in row.lower()]
        if (not signatures or any(not row.endswith(
                f"key fingerprint: {AMD_DNF_SIGNING_FINGERPRINT}: OK") for row in signatures)
                or any(not row.endswith(": OK") for row in details)
                or not any(re.fullmatch(r"Header SHA(?:256|512) digest: OK", row) for row in details)
                or not any(re.fullmatch(r"Payload SHA(?:256|512|3-256) digest: OK", row) for row in details)):
            raise RuntimeError("RPM signatures or strong digests do not match the Fedora 44 trusted key")


def cache_bytes(guest, label):
    output = guest(label, ("/usr/bin/du", "-s", "-B1", "--", NATIVE_ROOT, AMD_DNF_CACHE_ROOT),
                   timeout=8).stdout
    rows = [line.split("\t", 1) for line in output.splitlines()]
    if [path for _, path in rows] != [NATIVE_ROOT, AMD_DNF_CACHE_ROOT]:
        raise RuntimeError("native cache disk usage does not match the two approved paths")
    return {path: int(count) for count, path in rows}


def write_budget(prefetch, cache):
    existing = prefetch["reserved_write_bytes"]
    if (type(existing) is not int or existing < 2*49_794_425+AMD_DNF_EXACT_PLAN["download_bytes"]
            or type(prefetch["prior_write_bytes"]) is not int
            or prefetch["measured_download_with_response_headers"] > 200*MIB):
        raise RuntimeError("guarded prefetch accounting omits observed native cache allocations")
    payload = AMD_DNF_EXACT_PLAN["download_bytes"]
    installed = AMD_DNF_EXACT_PLAN["installed_bytes"]
    # Include a separate bounded allowance for native indexes, scriptlets and
    # evidence. The guard also enforces the cumulative observed 1 GiB ceiling.
    allowance = 64*MIB
    # The prefetch input is a reservation. Charge any observed excess over
    # its baseline exactly once instead of counting hard-linked raw data twice.
    adjustment = max(0, 49_794_425+sum(cache.values())-prefetch["prior_write_bytes"]-payload)
    projected = existing+adjustment+payload+installed+allowance
    if projected > 1024*MIB:
        raise RuntimeError("native package payload and installation exceed the cumulative disk budget")
    return {"prefetch_reserved_bytes": existing, "guest_allocated_cache_bytes": cache,
            "cache_hardlinks_counted_once": True, "new_guest_rpm_bytes": 0,
            "verified_existing_guest_rpm_bytes": payload,
            "prior_cache_accounting_adjustment_bytes": adjustment,
            "installed_bytes_from_primary": installed, "native_overhead_allowance_bytes": allowance,
            "projected_cumulative_write_bytes": projected}


def verify_cached_payload(guest, proof, cache_library):
    args, destinations, expected = [], [], []
    for row in proof["packages"]:
        key = cache_library["REPOS"][row["repository"]][1]
        target = f'{AMD_DNF_CACHE_ROOT}/{key}/packages/{Path(row["location"]).name}'
        args.extend((target, str(row["download_bytes"]), row["sha256"]))
        destinations.append(target)
        expected.append(f'{row["sha256"]} {row["download_bytes"]} {target}')
    output = guest("verify-cached-rpms", ("/bin/sh", "-ec", COPY, "unifier-rpm-cache-verify",
                   AMD_DNF_CACHE_ROOT, *args), timeout=18).stdout
    if output.splitlines() != expected:
        raise RuntimeError("guest cache checksum and size readbacks differ")
    return destinations


def prepare_and_install(service, fixture, spec, guest, out, report, proof, query, env):
    helpers = env["PKG"]
    cache = env["CACHE"]
    def checked_gpg_guest(label, argv, **options):
        if argv[0] == "/usr/bin/dnf":
            argv = (argv[0], "--setopt=gpgcheck=1", *argv[1:])
        return offline_guest(label, argv, **options)

    offline_guest = env["offline_guest"](guest, AMD_DNF_CACHE_ROOT)
    scoped_guest = checked_gpg_guest
    baseline = cache_bytes(guest, "cache-allocated-before")
    report["disk_budget"] = write_budget(proof["budget"], baseline)
    report["native_repo_config"] = env["native_config"](scoped_guest, AMD_DNF_CACHE_ROOT)
    repos = report["native_repo_config"]["repos"]
    sources = query["plan"]["sources"]
    if (not re.search(r"(?m)^gpgcheck\s*=\s*1[^\S\n]*$", report["native_repo_config"]["effective_main"])
            or repos.keys() != sources.keys() or any(
            row["url"] != sources[name]["metalink"] or row["gpgcheck"] is not True
            or row["repo_gpgcheck"] != (sources[name]["repo_gpgcheck"].lower() in {"1", "true", "yes"})
            for name, row in repos.items())):
        raise RuntimeError("native repository source or signing policy changed")
    key = guest("rpm-trusted-key", ("/usr/bin/rpm", "-q", "gpg-pubkey", "--queryformat",
                "%{VERSION}-%{RELEASE}\\n"), timeout=6).stdout.splitlines()
    if key != [f"{AMD_DNF_SIGNING_FINGERPRINT}-6786af3b"]:
        raise RuntimeError("the installed Fedora 44 RPM verification key differs")
    destinations = verify_cached_payload(guest, proof, cache)
    sig = guest("rpm-signatures", ("/usr/bin/rpmkeys", "--define", "_pkgverify_level all",
                "--checksig", "--verbose", *destinations), timeout=15)
    if sig.stderr.strip():
        raise RuntimeError("native RPM signature verification emitted a diagnostic")
    verify_signatures(sig.stdout, destinations)
    plan = helpers["make_plan"](scoped_guest, "amd", requests=AMD_DNF_EXACT_PLAN["requests"])
    if plan != query["plan"]:
        raise RuntimeError("native cached solver closure, version or repository differs")
    report["plan"] = plan
    attempt = service.runtime.attempt
    attempt.require_clean_packages()
    amd_install_submission_time(attempt)
    proof_ready = {"binding": AMD_DNF_BINDING, "prefetch_sha256": proof["prefetch_sha256"],
                   "query_sha256": proof["query_sha256"], "verified_rpms": proof["packages"],
                   "signing_fingerprint": AMD_DNF_SIGNING_FINGERPRINT, "solver_plan": plan}
    helpers["document"](out/"amd-offline-install-ready.json", proof_ready)
    names = package_names("amd", spec["package_plan"]["requests"])
    txn = service.install_packages(fixture.env_id, names)
    helpers["document"](out/"package-transaction.json", txn)
    report["transaction"] = {key: txn[key] for key in ("id", "phase", "verdict", "recovery_required")}
    if (txn.get("phase") != "completed" or txn.get("recovery_required") is not False
            or txn.get("environment_binding") != AMD_DNF_BINDING or not txn.get("verification", {}).get("ok")
            or len(txn["native"]) != 1 or txn["native"][0].get("exit_code") != 0):
        raise RuntimeError("formal offline Fedora package transaction did not complete")
    audit = guest("rpm-database-audit", ("/usr/bin/rpm", "--verifydb"), timeout=9)
    if audit.stdout.strip() or audit.stderr.strip():
        raise RuntimeError("native RPM database audit emitted a diagnostic")
    helpers["check_installed"](guest, "amd", plan["package_plan"]["requests"], plan["closure"], report)
    inventory = normalize_inventory(guest("rpm-inventory-after", inventory_argv(fixture.env_id),
                                          timeout=8).stdout)
    journal = guest("rpm-journal-probe", ("/bin/sh", "-c", package_journal.SCRIPT,
                    "unifier-package-journal", fixture.env_id, "probe"), timeout=6)
    if journal.stdout != "clean\n" or journal.stderr.strip():
        raise RuntimeError("native RPM journal is not clean after the formal transaction")
    report["native_journal"] = "clean"
    report["disk_budget"]["guest_allocated_cache_after_bytes"] = cache_bytes(guest, "cache-allocated-after")
    return inventory


def compare_formal_inventory(service, attempt, out, inventory):
    record, pending = attempt.pending_transactions()
    if (pending or record.get("package_recovery_required") is not False
            or record.get("runtime_state") != "stopped" or binding_for(record) != AMD_DNF_BINDING):
        raise RuntimeError("formal stopped record or transaction state differs")
    actual = bound_bytes(service.store.inventory_path(attempt.fixture.env_id))
    digest = hashlib.sha256(actual).hexdigest()
    if actual != inventory.encode() or record.get("package_inventory_sha256") != digest:
        raise RuntimeError("registered inventory in the stopped record differs from native post-install inventory")
    with (out/"verified-native-post-install-inventory.txt").open("xb") as stream:
        stream.write(actual)
    return {"sha256": digest, "entries": inventory.count("\n"), "registered_record_stopped": True}


def main():
    if len(sys.argv) != 1:
        raise RuntimeError("the fixed AMD offline controller takes no arguments")
    spec_path = Path(os.environ["UNIFIER_TEST_SPEC"])
    out = spec_path.parent
    report = {"schema": 1, "status": "FAIL", "cid": AMD_BOOTSTRAP_CID, "commands": [],
              "formal_stop": "NO_START", "recovery_required": False, "stopped_native_inventory": "NOT_RUN"}
    service = attempt = None
    inventory = None
    started = False
    try:
        service = make_service(spec_path)
        attempt = service.runtime.attempt
        spec, fixture = attempt.spec, attempt.fixture
        if spec.get("guest_profile") != AMD_DNF_INSTALL_RETRY_PROFILE:
            raise RuntimeError("the AMD controller requires the dedicated offline install profile")
        query = bound_read(AMD_DNF_SUCCEEDED_QUERY/"result.json")
        proof = bound_read(out/"amd-offline-install-before.json")
        env = amd_offline_libraries()
        guest = env["PKG"]["guest_runner"](service.runtime, attempt.active_config(), fixture, out, report)
        service.start(fixture.env_id, expected_binding=AMD_DNF_BINDING)
        started = True
        report["formal_stop"] = "UNKNOWN"
        inventory = prepare_and_install(service, fixture, spec, guest, out, report, proof, query, env)
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "detail": str(error)[:700]}
    finally:
        if started:
            try:
                service.stop(attempt.fixture.env_id)
                state = reuse.info(attempt.active_config(), hardware=attempt)["State"]
                if state["Running"] is not False or state["Pid"] != 0:
                    raise RuntimeError("formal stop did not confirm the bound CID was stopped")
                report["formal_stop"] = "STOPPED"
            except Exception as error:
                report["stop_error"] = str(error)[:700]
        if attempt:
            try:
                record, pending = attempt.pending_transactions()
                report["recovery_required"] = bool(pending or record.get("package_recovery_required"))
                if (inventory is not None and report["formal_stop"] == "STOPPED"
                        and not report["recovery_required"] and not report.get("error")):
                    report["verified_inventory"] = compare_formal_inventory(service, attempt, out, inventory)
                    report["status"] = "PASS"
            except Exception as error:
                report["verification_error"] = str(error)[:700]
        reuse.put(out/"result.json", report)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
