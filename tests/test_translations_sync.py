"""Shipped translations must match what translations_src expands to."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "generate_translations.py"


def test_translations_are_generated_and_deduplicated() -> None:
    """Fail if translations/*.json were edited by hand or a source repeats a string."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
