"""Import-discipline guards for the daemon public package."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_daemon_public_import_stays_fast() -> None:
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import time; "
                "t=time.perf_counter(); "
                "from screencap import daemon; "
                "print(time.perf_counter()-t)"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
        check=True,
    )
    elapsed = float(proc.stdout.strip())
    assert elapsed < 0.05
