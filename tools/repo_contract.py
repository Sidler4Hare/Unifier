"""Offline checks for supported entry points and known product dependencies."""

import ast
import json
from importlib.util import resolve_name


def load_contract(root):
    return json.loads((root / "config/repository-contract.json").read_text(encoding="utf-8"))


def verify_entrypoints(root, contract):
    missing = []
    aliases = set(contract["legacy_aliases"]["modules"])
    alias_dir = root / "src/multilinux"
    for module in sorted(aliases):
        for path in (alias_dir / f"{module}.py", root / "src/unifier" / f"{module}.py"):
            if not path.is_file():
                missing.append(f"legacy alias multilinux.{module} requires {path.relative_to(root)}")
    discovered = {path.stem for path in alias_dir.glob("*.py")} - {"__init__", "__main__"}
    for module in sorted(discovered - aliases):
        missing.append(f"legacy alias multilinux.{module} is not registered")

    for entry in contract["entrypoints"]:
        context = f"{entry['name']} ({', '.join(entry['callers'])})"
        for name in entry["files"] + entry["callers"] + entry["tests"]:
            if not (root / name).is_file():
                missing.append(f"{context} requires {name}")
        inputs = [root / name for name in entry.get("candidate_inputs", [])]
        if any(path.exists() or path.is_symlink() for path in inputs):
            for path in inputs:
                if not path.is_file():
                    missing.append(f"{context} has incomplete candidate inputs: {path.relative_to(root)}")
    if missing:
        raise ValueError("\n".join(missing))
    print(f"PASS entry-point inventory: {len(contract['entrypoints'])} entries, "
          f"{len(aliases)} legacy aliases", flush=True)


def imported_names(module, tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            package = node.module or ""
            if node.level:
                package = resolve_name("." * node.level + package, module.rpartition(".")[0])
            if package not in {"tools", "multilinux"}:
                yield package
            if package.partition(".")[0] in {"unifier", "tools", "multilinux"}:
                yield from (f"{package}.{alias.name}" for alias in node.names)


def reachable(graph, start):
    visited = set()
    pending = [start]
    while pending:
        module = pending.pop()
        if module not in visited:
            visited.add(module)
            pending.extend(graph[module] - visited)
    return visited


def verify_import_directions(trees, contract):
    graph = {module: set() for module in trees}
    outward = set()
    for module, tree in trees.items():
        for name in imported_names(module, tree):
            if name.partition(".")[0] in {"tools", "multilinux"}:
                outward.add((module, ".".join(name.split(".")[:2])))
            else:
                dependency = name if name in graph else f"{name}.__init__"
                if dependency in graph and dependency != module:
                    graph[module].add(dependency)

    reach = {module: reachable(graph, module) for module in graph}
    cyclic = {(module, dependency) for module, dependencies in graph.items()
              for dependency in dependencies if module in reach[dependency]}
    baseline = contract["import_baseline"]
    new_outward = outward - {tuple(edge) for edge in baseline["outward"]}
    new_cycles = cyclic - {tuple(edge) for edge in baseline["cyclic"]}
    if new_outward or new_cycles:
        details = [f"new product import: {left} -> {right}" for left, right in sorted(new_outward)]
        details.extend(f"new cycle edge: {left} -> {right}" for left, right in sorted(new_cycles))
        raise ValueError("\n".join(details))
    print(f"PASS import directions: {len(outward)} existing outward imports, "
          f"{len(cyclic)} existing cycle edges", flush=True)
