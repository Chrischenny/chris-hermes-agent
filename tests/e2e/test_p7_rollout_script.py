from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROLLOUT_SCRIPT = PROJECT_ROOT / "scripts" / "chris-avatar-rollout.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _runtime(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    hermes_home = tmp_path / "hermes"
    profile = hermes_home / "profiles" / "chris-avatar"
    (profile / "sessions").mkdir(parents=True)
    (profile / "config.yaml").write_text("context:\n  engine: compressor\n")
    (profile / "SOUL.md").write_text("original soul\n")
    (profile / "sessions" / "request.json").write_text("{}\n")
    connection = sqlite3.connect(profile / "state.db")
    connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    connection.execute("INSERT INTO marker VALUES ('before-rollout')")
    connection.commit()
    connection.close()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    command_log = tmp_path / "commands.log"
    _write_executable(
        fake_bin / "hermes",
        '#!/bin/sh\nprintf \'hermes %s\\n\' "$*" >> "$P7_COMMAND_LOG"\n',
    )
    _write_executable(
        fake_bin / "systemctl",
        '#!/bin/sh\nprintf \'systemctl %s\\n\' "$*" >> "$P7_COMMAND_LOG"\n'
        'case "$*" in\n'
        "  *is-active*) printf 'active\\n' ;;\n"
        "  *show*) printf 'MainPID=123\\nActiveState=active\\n' ;;\n"
        "esac\n",
    )
    env = {
        "HOME": str(tmp_path / "home"),
        "HERMES_BIN": str(fake_bin / "hermes"),
        "HERMES_HOME": str(hermes_home),
        "HERMES_PROFILE": "chris-avatar",
        "LANG": "C.UTF-8",
        "PATH": os.environ.get("PATH", ""),
        "P7_COMMAND_LOG": str(command_log),
        "SQLITE3_BIN": shutil.which("sqlite3") or "sqlite3",
        "SYSTEMCTL_BIN": str(fake_bin / "systemctl"),
        "TZ": "UTC",
    }
    return env, profile, command_log


def test_backup_and_one_command_rollback_preserve_diagnostics(tmp_path: Path) -> None:
    env, profile, command_log = _runtime(tmp_path)
    backup = tmp_path / "backup"

    backed_up = subprocess.run(
        [str(ROLLOUT_SCRIPT), "backup", str(backup)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert backed_up.returncode == 0, backed_up.stdout + backed_up.stderr
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    assert (backup / "config.yaml").read_text() == "context:\n  engine: compressor\n"
    assert (backup / "SOUL.md").read_text() == "original soul\n"
    assert (backup / "session-index.txt").read_text() == "request.json\n"
    with sqlite3.connect(backup / "state.db") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == (
            "before-rollout"
        )

    (profile / "config.yaml").write_text("context:\n  engine: context-handoff\n")
    (profile / "SOUL.md").write_text("changed soul\n")
    with sqlite3.connect(profile / "state.db") as connection:
        connection.execute("UPDATE marker SET value = 'after-rollout'")
        connection.commit()
    diagnostics = profile / "plugin-data" / "chris-hermes-agent" / "archives"
    diagnostics.mkdir(parents=True)
    (diagnostics / "evidence.json").write_text("{}\n")

    rolled_back = subprocess.run(
        [str(ROLLOUT_SCRIPT), "rollback", str(backup)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert rolled_back.returncode == 0, rolled_back.stdout + rolled_back.stderr
    assert (profile / "config.yaml").read_text() == "context:\n  engine: compressor\n"
    assert (profile / "SOUL.md").read_text() == "original soul\n"
    with sqlite3.connect(profile / "state.db") as connection:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == (
            "before-rollout"
        )
    assert (diagnostics / "evidence.json").is_file()
    commands = command_log.read_text()
    assert "systemctl --user stop hermes-gateway-chris-avatar.service" in commands
    assert "hermes -p chris-avatar plugins disable chris-hermes-agent" in commands
    assert "systemctl --user start hermes-gateway-chris-avatar.service" in commands


def test_rollback_rejects_an_incomplete_backup(tmp_path: Path) -> None:
    env, _, command_log = _runtime(tmp_path)
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()

    result = subprocess.run(
        [str(ROLLOUT_SCRIPT), "rollback", str(incomplete)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode != 0
    assert "not a complete chris-avatar backup" in result.stderr
    assert not command_log.exists()
