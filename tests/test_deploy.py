"""`insta-outreach setup`: .env token, service definitions per OS."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from insta_outreach.deploy import SERVICE_LABEL, ensure_env_file, service_plan

ROOT = Path("/Users/rohit/insta & outreach")  # a space and an XML special character on purpose
PYTHON = ROOT / ".venv" / "bin" / "python"


def test_env_file_gets_a_token_once_and_never_loses_the_users(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("# comment\nCONTROL_API_TOKEN=\nIG_USER_ID=\n", encoding="utf-8")
    created, token = ensure_env_file(tmp_path)
    assert created and token and len(token) >= 40
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert f"CONTROL_API_TOKEN={token}\n" in text and "# comment\n" in text and "IG_USER_ID=\n" in text
    assert ensure_env_file(tmp_path) == (False, None)  # re-running keeps it
    (tmp_path / ".env").write_text("export CONTROL_API_TOKEN='mine'\n", encoding="utf-8")
    assert ensure_env_file(tmp_path) == (False, None)
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "export CONTROL_API_TOKEN='mine'\n"
    (tmp_path / ".env").write_text("OTHER=1\n", encoding="utf-8")
    _, appended = ensure_env_file(tmp_path)
    assert (tmp_path / ".env").read_text(encoding="utf-8") == f"OTHER=1\nCONTROL_API_TOKEN={appended}\n"


def test_macos_launch_agent_is_valid_and_keeps_the_mac_awake() -> None:
    plan = service_plan("darwin", ROOT, PYTHON, Path("/Users/rohit"))
    [(path, content)] = plan.files.items()
    assert path == Path(f"/Users/rohit/Library/LaunchAgents/{SERVICE_LABEL}.plist")
    agent = plistlib.loads(content.encode())
    assert agent["WorkingDirectory"] == str(ROOT)
    assert agent["ProgramArguments"] == ["/usr/bin/caffeinate", "-i", str(PYTHON), "-m", "insta_outreach", "run"]
    assert agent["KeepAlive"] is True and agent["RunAtLoad"] is True
    assert plan.start == [f'launchctl bootstrap gui/$(id -u) "{path}"']


def test_linux_user_unit_and_windows_logon_task() -> None:
    linux = service_plan("linux", ROOT, PYTHON, Path("/home/rohit"))
    [(unit, content)] = linux.files.items()
    assert unit == Path("/home/rohit/.config/systemd/user/insta-outreach.service")
    assert f"WorkingDirectory={ROOT}\n" in content and f'ExecStart="{PYTHON}" -m insta_outreach run' in content
    assert "systemctl --user enable --now insta-outreach" in linux.start

    root = Path("C:/Users/Rohit/insta-outreach")
    windows = service_plan("win32", root, root / ".venv/Scripts/python.exe", Path("C:/Users/Rohit"))
    [(launcher, script)] = windows.files.items()
    assert launcher == root / "data" / "run-outreach.cmd"
    assert f'cd /d "{root}"' in script and "-m insta_outreach run >>" in script
    assert windows.start[0].startswith('schtasks /Create /TN "insta-outreach"') and "/SC ONLOGON" in windows.start[0]
    with pytest.raises(ValueError):
        service_plan("sunos5", root, PYTHON, Path("/"))
