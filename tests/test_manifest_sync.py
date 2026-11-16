"""manifest.json requirements must match the dependencies tested in pyproject."""

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_manifest_requirements_match_pyproject() -> None:
    manifest = json.loads(
        (ROOT / "custom_components" / "feels_like" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert sorted(manifest["requirements"]) == sorted(
        pyproject["project"]["dependencies"]
    )
