"""Layer import guard for the hexagonal refactoring.

Walks every .py file under the four layer roots and flags imports that
violate the dependency rule:

    api      -> di, core                    (NOT services directly)
    di       -> core, services              (free across boundaries)
    services -> core                        (NOT api, NOT di)
    core     -> (nothing in openrag)        (pure domain)

Only files inside openrag/{core,services,api,di}/ are checked. Legacy
paths (openrag/components/, openrag/routers/, openrag/models/, ...) are
ignored until they are migrated.

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
    if src_layer is None:
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return [f"{path}:{exc.lineno}  syntax error in file: {exc.msg}"]

    violations: list[str] = []
    forbidden = FORBIDDEN[src_layer]
    for lineno, entry in iter_imports(tree):
        if isinstance(entry, tuple) and entry and entry[0] == "__relative__":
            _, level, module = entry
            dotted = resolve_relative(path, level, module)
        else:
            dotted = entry
        tgt_layer = layer_of(dotted)
        if tgt_layer is None or tgt_layer == src_layer:
            continue
        if tgt_layer in forbidden:
            rel = path.relative_to(REPO_ROOT)
            violations.append(f"{rel}:{lineno}  {src_layer} -> {tgt_layer}  ({dotted})")
    return violations


def main() -> int:
    if not OPENRAG.is_dir():
        print(f"error: {OPENRAG} not found", file=sys.stderr)
        return 2

    all_violations: list[str] = []
    for path in sorted(OPENRAG.rglob("*.py")):
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
