"""Layer import guard for the hexagonal refactoring.

Walks every .py file under the four layer roots and flags imports that
violate the dependency rule:

    api      -> di, core                    (NOT services directly)
    di       -> core, services              (free across boundaries)
    services -> core                        (NOT api, NOT di)
    core     -> (nothing in openrag)        (pure domain)

Layer boundaries are checked only inside openrag/{core,services,api,di}/.

Two third-party rules apply inside openrag/ as well:

    langgraph               -> only in openrag/services/chat/graphs/
    langchain_openai,
    langchain_community,
    langchain_core.messages -> not in openrag/services/
    langgraph_sdk,
    langgraph.pregel.remote -> nowhere in openrag/

LangGraph only wires the chat workflows; the nodes call OpenRag's own LLM
port with OpenAI-style message dicts, never LangChain's model layer.
langgraph-sdk, the LangGraph Platform client langgraph depends on, is never
loaded: .trivyignore.yaml accepts its advisories as unreachable on that basis.

Absolute application imports must use the bare layer root, not ``openrag.``:
mixing the two loads distinct copies of classes such as exception types.
This import-root rule covers all Python files in openrag/, tests/, and scripts/.

Usage:
    python scripts/check_layer_imports.py

Exit code 0 on pass, 1 on any violation. Prints one line per violation:
    path/to/file.py:LINE  core -> services  (openrag.services.foo)
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OPENRAG = REPO_ROOT / "openrag"

LAYERS = ("core", "services", "api", "di")

FORBIDDEN: dict[str, set[str]] = {
    "core": {"services", "api", "di"},
    "services": {"api", "di"},
    "api": {"services"},
    "di": set(),
}


#: Third-party modules importable only under these openrag/ sub-paths.
CONFINED_THIRD_PARTY: dict[str, tuple[str, ...]] = {
    "langgraph": ("services/chat/graphs",),
}

#: Third-party modules a layer must not import.
BANNED_THIRD_PARTY: dict[str, tuple[str, ...]] = {
    "services": ("langchain_openai", "langchain_community", "langchain_core.messages"),
}

#: Third-party modules nothing in openrag/ may import. langgraph.pregel.remote
#: is langgraph's only importer of langgraph_sdk.
UNUSED_THIRD_PARTY: tuple[str, ...] = ("langgraph_sdk", "langgraph.pregel.remote")


def _is_module_or_submodule(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def layer_of(module: str) -> str | None:
    """Return the layer name if `module` is openrag.<layer>[...] or <layer>[...], else None."""
    parts = module.split(".")
    if len(parts) >= 2 and parts[0] == "openrag" and parts[1] in LAYERS:
        return parts[1]
    if parts[0] in LAYERS:
        return parts[0]
    return None


def file_layer(path: Path) -> str | None:
    """Return the layer the file belongs to, or None if outside the four roots."""
    try:
        rel = path.relative_to(OPENRAG)
    except ValueError:
        return None
    top = rel.parts[0] if rel.parts else ""
    return top if top in LAYERS else None


def iter_imports(tree: ast.AST):
    """Yield (lineno, dotted_module) for every Import/ImportFrom node."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # relative import — resolve against the file's package later
                yield node.lineno, ("__relative__", node.level, node.module or "")
            else:
                yield node.lineno, node.module or ""


def iter_absolute_imports(tree: ast.AST):
    """Yield (lineno, dotted_name) for every absolute import, ``from`` names included.

    ``from langchain_core import messages`` yields ``langchain_core`` and
    ``langchain_core.messages``, so a submodule imported by name is caught too.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            yield node.lineno, node.module
            for alias in node.names:
                yield node.lineno, f"{node.module}.{alias.name}"


def check_third_party(path: Path, tree: ast.AST) -> list[str]:
    """Violations of CONFINED_THIRD_PARTY / BANNED_THIRD_PARTY / UNUSED_THIRD_PARTY in an openrag/ file."""
    try:
        within = path.relative_to(OPENRAG).as_posix()
    except ValueError:
        return []
    src_layer = file_layer(path)
    rel = path.relative_to(REPO_ROOT)
    violations: list[str] = []
    seen: set[tuple[int, str]] = set()
    for lineno, dotted in iter_absolute_imports(tree):
        for prefix, allowed in CONFINED_THIRD_PARTY.items():
            if _is_module_or_submodule(dotted, prefix) and not any(
                within.startswith(sub_path + "/") for sub_path in allowed
            ):
                if (lineno, prefix) not in seen:
                    seen.add((lineno, prefix))
                    places = ", ".join(f"openrag/{sub_path}/" for sub_path in allowed)
                    violations.append(f"{rel}:{lineno}  {prefix} is only allowed in {places}  ({dotted})")
        for prefix in BANNED_THIRD_PARTY.get(src_layer, ()):
            if _is_module_or_submodule(dotted, prefix) and (lineno, prefix) not in seen:
                seen.add((lineno, prefix))
                violations.append(f"{rel}:{lineno}  {src_layer} must not import {prefix}  ({dotted})")
        for prefix in UNUSED_THIRD_PARTY:
            if _is_module_or_submodule(dotted, prefix) and (lineno, prefix) not in seen:
                seen.add((lineno, prefix))
                violations.append(f"{rel}:{lineno}  {prefix} must not be imported in openrag/  ({dotted})")
    return violations


def resolve_relative(file_path: Path, level: int, module: str) -> str:
    """Turn `from ..foo import bar` into an absolute dotted module."""
    rel = file_path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(rel.parts)
    # __init__.py sits one level shallower
    if parts[-1] == "__init__":
        parts.pop()
    # `level` dots = climb that many packages
    anchor = parts[:-level] if level <= len(parts) else []
    if module:
        anchor.extend(module.split("."))
    return ".".join(anchor)


def check_file(path: Path) -> list[str]:
    src_layer = file_layer(path)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return [f"{path}:{exc.lineno}  syntax error in file: {exc.msg}"]

    violations: list[str] = []
    forbidden = FORBIDDEN.get(src_layer, set())
    for lineno, entry in iter_imports(tree):
        if isinstance(entry, tuple) and entry and entry[0] == "__relative__":
            if src_layer is None:
                continue
            _, level, module = entry
            dotted = resolve_relative(path, level, module)
        else:
            dotted = entry
            if dotted == "openrag" or dotted.startswith("openrag."):
                rel = path.relative_to(REPO_ROOT)
                violations.append(f"{rel}:{lineno}  use the bare import root, not {dotted} (#885)")
                continue
        tgt_layer = layer_of(dotted)
        if tgt_layer is None or tgt_layer == src_layer:
            continue
        if tgt_layer in forbidden:
            rel = path.relative_to(REPO_ROOT)
            violations.append(f"{rel}:{lineno}  {src_layer} -> {tgt_layer}  ({dotted})")
    violations.extend(check_third_party(path, tree))
    return violations


def main() -> int:
    if not OPENRAG.is_dir():
        print(f"error: {OPENRAG} not found", file=sys.stderr)
        return 2

    all_violations: list[str] = []
    for root in (OPENRAG, REPO_ROOT / "tests", REPO_ROOT / "scripts"):
        for path in sorted(root.rglob("*.py")):
            all_violations.extend(check_file(path))

    if all_violations:
        print("layer import violations:")
        for v in all_violations:
            print(f"  {v}")
        print(f"\n{len(all_violations)} violation(s)")
        return 1

    print("layer import guard: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
