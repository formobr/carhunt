"""Run the real deploy scripts with stubbed systemctl/claude on PATH."""

import os
import subprocess
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
UNITS = [
    "carhunt-poll.service",
    "carhunt-poll.timer",
    "carhunt-summary.service",
    "carhunt-summary.timer",
]


def _run(
    script: str, tmp_path: Path, extra_env: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess, Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "systemctl.calls"
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\n')
    claude = bin_dir / "claude"
    claude.write_text("#!/bin/sh\nexit 0\n")
    systemctl.chmod(0o755)
    claude.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "XDG_CONFIG_HOME": str(tmp_path),
        **(extra_env or {}),
    }
    proc = subprocess.run(
        ["bash", str(DEPLOY / script)],
        check=False,
        env=env,
        capture_output=True,
        text=True,
    )
    return proc, bin_dir, calls


def _calls(calls: Path) -> list[str]:
    return calls.read_text().splitlines()


def test_install_renders_units_and_enables_timers(tmp_path):
    proc, bin_dir, calls = _run("install.sh", tmp_path)
    assert proc.returncode == 0, proc.stderr

    dest = tmp_path / "systemd" / "user"
    for name in UNITS:
        text = (dest / name).read_text()
        assert "@CLAUDE_DIR@" not in text
        assert "@INTERVAL_MIN@" not in text
        assert "@TIMEZONE@" not in text

    assert "OnUnitActiveSec=15min" in (dest / "carhunt-poll.timer").read_text()
    assert "09:00:00 America/New_York" in (dest / "carhunt-summary.timer").read_text()
    assert str(bin_dir) in (dest / "carhunt-poll.service").read_text()

    lines = _calls(calls)
    assert "--user daemon-reload" in lines
    assert "--user enable --now carhunt-poll.timer carhunt-summary.timer" in lines


def test_install_renders_summary_timer_in_report_timezone(tmp_path):
    proc, _, _ = _run(
        "install.sh", tmp_path, {"CARHUNT__REPORT__TIMEZONE": "America/Los_Angeles"}
    )
    assert proc.returncode == 0, proc.stderr
    timer = (tmp_path / "systemd" / "user" / "carhunt-summary.timer").read_text()
    assert "OnCalendar=*-*-* 09:00:00 America/Los_Angeles" in timer
    assert "New_York" not in timer


def test_uninstall_removes_units(tmp_path):
    dest = tmp_path / "systemd" / "user"
    dest.mkdir(parents=True)
    for name in UNITS:
        (dest / name).write_text("x")

    proc, _, calls = _run("uninstall.sh", tmp_path)
    assert proc.returncode == 0, proc.stderr
    for name in UNITS:
        assert not (dest / name).exists()
    assert "--user daemon-reload" in _calls(calls)
