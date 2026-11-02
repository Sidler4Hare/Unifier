"""Fresh, policy-aware roots for opt-in Podman integration tests."""

from pathlib import Path
import shutil
import tempfile
import unittest

from unifier.compatibility import observe_host, relationship_plan
from unifier.project import load_project
from unifier.runtime import PodmanRuntime
from unifier.service import EnvironmentService

# Opt-in guests write their receipt to <root>/evidence when MULTILINUX_EVIDENCE=1 and
# UNIFIER_TEST_EVIDENCE is unset.  This repo-side home keeps that receipt after the
# temporary root is removed; offline tests patch it so nothing lands in real artifacts.
EVIDENCE_ROOT = Path(__file__).resolve().parents[1] / "artifacts" / "integration-runs"
DEFAULT_EVIDENCE_DIR = "evidence"


def admitted_targets(config, targets, *, host_facts=None):
    facts = observe_host() if host_facts is None else host_facts
    plans = {item[1]: relationship_plan(facts, item[1], config["targets"][item[1]])
             for item in targets}
    selected = tuple(item for item in targets if plans[item[1]]["allowed"])
    if not selected:
        reasons = "; ".join(reason for plan in plans.values() for reason in plan["reasons"])
        raise unittest.SkipTest(f"no admitted integration guests: {reasons}")
    return selected, plans


def preserve_default_evidence(root):
    """Copy a default-receipt directory out before its temporary root is removed."""
    source = Path(root) / DEFAULT_EVIDENCE_DIR
    if not source.is_dir():
        return None
    destination = EVIDENCE_ROOT / Path(root).name
    try:
        EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
        # An existing run directory is never replaced.
        shutil.copytree(source, destination)
    except (OSError, shutil.Error) as exc:
        raise RuntimeError(f"integration evidence could not be preserved at {destination}; "
                           f"temporary root retained at {root}") from exc
    return destination


def isolated_service(testcase, label):
    config, lock, _ = load_project()
    root = Path(tempfile.mkdtemp(prefix=f"unifier-{label}-"))
    runtime = PodmanRuntime()
    service = EnvironmentService(root, runtime, config, lock)

    def cleanup():
        if service.store.all():
            raise RuntimeError(f"integration cleanup incomplete; state retained at {root}")
        preserve_default_evidence(root)
        shutil.rmtree(root)

    testcase.addCleanup(cleanup)
    return service, runtime, root
