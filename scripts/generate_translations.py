"""Expand ``translations_src/<lang>.json`` into the shipped translation files.

Home Assistant does not resolve ``[%key:...%]`` references for custom
integrations, so the files in ``custom_components/feels_like/translations`` must
be complete. To avoid repeating the same string many times, edit the sources in
``translations_src`` instead: a value of the form ``"[%key:a::b::c%]"`` is
replaced with the value (string or whole subtree) found at that path in the same
file.

Usage:
    python scripts/generate_translations.py          # (re)write the generated files
    python scripts/generate_translations.py --check  # fail if out of date or if a
                                                     # source repeats a string
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "translations_src"
OUT_DIR = ROOT / "custom_components" / "feels_like" / "translations"

_REF = re.compile(r"^\[%key:([^%]+)%\]$")


def _lookup(root: Any, path: list[str], stack: tuple[str, ...]) -> Any:
    """Return the (expanded) node at ``path``, following references on the way."""
    node = root
    for index, part in enumerate(path):
        # Follow a reference sitting on the path without expanding the siblings.
        while isinstance(node, str) and (match := _REF.match(node)):
            ref = match.group(1)
            if ref in stack:
                raise ValueError(f"Reference cycle: {' -> '.join((*stack, ref))}")
            stack = (*stack, ref)
            node = _raw(root, ref.split("::"))
        if not isinstance(node, dict) or part not in node:
            raise KeyError("::".join(path[: index + 1]))
        node = node[part]
    return _expand(node, root, stack)


def _raw(root: Any, path: list[str]) -> Any:
    """Return the unexpanded node at ``path`` (references are not followed)."""
    node = root
    for index, part in enumerate(path):
        if not isinstance(node, dict) or part not in node:
            raise KeyError("::".join(path[: index + 1]))
        node = node[part]
    return node


def _expand(node: Any, root: Any, stack: tuple[str, ...] = ()) -> Any:
    if isinstance(node, str):
        match = _REF.match(node)
        if match is None:
            return node
        ref = match.group(1)
        if ref in stack:
            raise ValueError(f"Reference cycle: {' -> '.join((*stack, ref))}")
        try:
            return _lookup(root, ref.split("::"), (*stack, ref))
        except KeyError as err:
            raise KeyError(f"Reference [%key:{ref}%] missing at {err}") from None
    if isinstance(node, dict):
        return {key: _expand(value, root, stack) for key, value in node.items()}
    return node


def render(source: dict[str, Any]) -> str:
    """Expand all references and serialise like the hand-written files."""
    return json.dumps(_expand(source, source), indent=2, ensure_ascii=False) + "\n"


def duplicates(source: Any) -> dict[str, list[str]]:
    """Literal string values (not references) that occur more than once."""
    seen: dict[str, list[str]] = defaultdict(list)

    def walk(node: Any, path: tuple[str, ...]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, (*path, key))
        elif isinstance(node, str) and not _REF.match(node):
            seen[node].append("::".join(path))

    walk(source, ())
    return {value: paths for value, paths in seen.items() if len(paths) > 1}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check", action="store_true", help="do not write, only verify"
    )
    args = parser.parse_args()

    status = 0
    for src in sorted(SRC_DIR.glob("*.json")):
        source = json.loads(src.read_text(encoding="utf-8"))
        out = OUT_DIR / src.name
        text = render(source)

        if args.check:
            current = out.read_text(encoding="utf-8") if out.exists() else None
            if current != text:
                print(f"{out.relative_to(ROOT)} is out of date; regenerate it")
                status = 1
            for value, paths in duplicates(source).items():
                print(f"{src.name}: repeated string {value!r} at {', '.join(paths)}")
                status = 1
        else:
            out.write_text(text, encoding="utf-8", newline="\n")
            print(f"wrote {out.relative_to(ROOT)}")
    return status


if __name__ == "__main__":
    sys.exit(main())
