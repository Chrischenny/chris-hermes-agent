from __future__ import annotations

import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_official_hermes_plugin_doctor_accepts_repository() -> None:
    hermes_cli = os.environ.get("HERMES_CLI", "hermes")

    result = subprocess.run(
        [hermes_cli, "plugins", "doctor", str(PROJECT_ROOT), "--ci"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "registrations: 3 tool(s)" in result.stdout
    assert "WARN:" not in result.stdout
