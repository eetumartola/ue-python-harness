"""Thin wrapper for ue_py_harness.py.

Usage:
    python scripts/run_harness.py discover --timeout-sec 2
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    scripts_dir = Path(__file__).resolve().parent
    harness_script = scripts_dir / "ue_py_harness.py"

    if not harness_script.exists():
        print(
            f"Harness script not found: {harness_script}",
            file=sys.stderr,
        )
        return 7

    cmd = [sys.executable, str(harness_script), *argv]
    proc = subprocess.run(cmd)
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
