"""Turn bounded native solver output into guest-scoped installation guidance."""

from __future__ import annotations

import re
import shlex

from unifier.packages import PACKAGE_NAME


_APT_NAME = r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}(?::[a-z0-9][a-z0-9_-]{0,31})?"
_NAME = r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}"
_CAPABILITY = r"[A-Za-z0-9/][A-Za-z0-9/._+:()=<>~@ -]{0,159}"
_RULES = {
    "debian13": (
        ("missing-package", rf"(?:E:\s+)?Unable to locate package ({_APT_NAME})"),
        ("missing-package", rf"(?:E:\s+)?Package ['\"]?({_APT_NAME})['\"]? has no installation candidate"),
        ("missing-dependency", rf"Package ({_APT_NAME}) is not installed\."),
        ("dependency-conflict", rf"(?:{_APT_NAME}\s+:\s+)?(?:PreDepends|Pre-Depends|Depends):"
         rf"\s+({_APT_NAME})(?: \([^)]{{1,96}}\))? but .{{1,200}}"),
        ("missing-hexdump", r"[^:\r\n]{1,240}:\s*(?:[0-9]{1,8}:\s*)?(hexdump): (?:command )?not found"),
    ),
    "fedora44": (
        ("missing-package", rf"(?:Failed to resolve (?:the )?transaction:\s+)?No match for argument:\s+({_NAME})"),
        ("dependency-conflict", rf"(?:[-*]\s+)?nothing provides ({_CAPABILITY}) needed by [^\r\n]{{1,200}}"),
    ),
    "arch": (
        ("missing-package", rf"error: target not found:\s+({_NAME})"),
        ("dependency-conflict", rf"(?::: )?unable to satisfy dependency '({_CAPABILITY})' required by [^\r\n]{{1,200}}"),
        ("dependency-conflict", rf"(?::: )?installing [^\r\n]{{1,160}} breaks dependency '({_CAPABILITY})' required by [^\r\n]{{1,200}}"),
    ),
}
_PATTERNS = {name: tuple((kind, re.compile(pattern)) for kind, pattern in rules)
             for name, rules in _RULES.items()}
_COLOR = re.compile(r"\x1b\[[0-9;]{0,32}m")
_MAX_INPUT = 128 * 1024
_MAX_FINDINGS = 12
_TXN_ID = re.compile(r"txn[a-f0-9]{12}\Z")


def native_findings(target: str, stdout: str, stderr: str) -> list[dict]:
    """Recognize only specific native messages, preserving the saved raw output."""
    rules = _PATTERNS.get(target, ())
    findings = []
    for stream in (stderr, stdout):
        for line in _COLOR.sub("", stream[-_MAX_INPUT:]).replace("\r", "\n").split("\n"):
            line = line.strip()
            for kind, rule in rules:
                match = rule.fullmatch(line)
                if match:
                    item = {"kind": kind, "name": match[1]}
                    if item not in findings:
                        findings.append(item)
                    break
            if len(findings) == _MAX_FINDINGS:
                return findings
    return findings


def _lookup(target, env_id, kind, name):
    prefix = ["env", "run", env_id, "--"]
    if target == "debian13":
        return [*prefix, "apt-cache", "policy", name]
    if target == "fedora44":
        query = ["--whatprovides=" + name] if kind == "dependency-conflict" else [name]
        return [*prefix, "dnf", "repoquery", "--available", *query]
    package = re.split(r"[<=>]", name, maxsplit=1)[0]
    if PACKAGE_NAME.fullmatch(package):
        return [*prefix, "pacman", "-Si", package]
    return None


def installation_guidance(txn: dict, target: str) -> dict | None:
    """Use solver facts and repository-backed providers from this transaction."""
    if txn.get("phase") in (None, "completed", "running"):
        return None
    from unifier.dependencies import LIBRARY

    findings = list(txn.get("native_dependencies") or [])
    commands = []
    env_id = txn["environment_id"]
    install = ["pkg", "install", env_id]
    repair = txn.get("dependency_repair") or {}
    rounds = repair.get("rounds") or []
    providers = (rounds[-1].get("providers") or {}) if rounds else {}
    for name in (repair.get("unresolved") or [])[:_MAX_FINDINGS]:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,254}", name):
            continue
        choices = [item for item in providers.get(name, ())
                   if isinstance(item, str) and PACKAGE_NAME.fullmatch(item)][:_MAX_FINDINGS]
        findings.append({"kind": "missing-library", "name": name, "providers": choices})
        for package in choices:
            commands.append({"kind": "install-provider", "argv": [*install, package]})
        if LIBRARY.fullmatch(name):
            commands.append({"kind": "repair-library",
                             "argv": ["pkg", "repair-deps", env_id, "--library", name]})
    for item in (txn.get("native_dependencies") or [])[:_MAX_FINDINGS]:
        name = item["name"]
        kind = item["kind"]
        if kind == "missing-hexdump":
            commands.append({"kind": "install-dependency", "argv": [*install, "bsdextrautils"]})
        elif kind == "missing-dependency" and PACKAGE_NAME.fullmatch(name):
            commands.append({"kind": "install-dependency", "argv": [*install, name]})
        elif argv := _lookup(target, env_id, kind, name):
            commands.append({"kind": "lookup-package", "argv": argv})

    if txn.get("recovery_required") or txn["phase"] == "needs-recovery":
        commands = [{"kind": "recover", "argv": ["pkg", "recover", env_id]}]
    if txn_id := txn.get("id"):
        if _TXN_ID.fullmatch(txn_id):
            commands.append({"kind": "show-transaction", "argv": ["pkg", "show", txn_id]})
    distinct = []
    for command in commands:
        if command not in distinct:
            distinct.append(command)
    return {"target": target, "findings": findings, "commands": distinct}


def guidance_messages(translate, guidance: dict, root) -> list[str]:
    """Messages for both installer surfaces. Structured argv remains in JSON."""
    values = []
    prefix = ["env", f"UNIFIER_HOME={root}", "unifier"]
    for finding in guidance["findings"]:
        values.append(translate("install.finding." + finding["kind"], name=finding["name"]))
        if finding.get("providers"):
            values.append(translate("install.finding.providers", names=", ".join(finding["providers"])))
    lookup = next((command for command in guidance["commands"]
                   if command["kind"] == "lookup-package"), None)
    if lookup:
        env_id = lookup["argv"][2]
        values.append(translate("install.hint.start-lookup",
                                command=shlex.join([*prefix, "env", "start", env_id])))
    for command in guidance["commands"]:
        values.append(translate("install.hint." + command["kind"],
                                command=shlex.join([*prefix, *command["argv"]])))
    return values
