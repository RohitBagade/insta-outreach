"""Local deployment helpers behind ``insta-outreach setup``.

Everything here writes only to the project folder or the user's own service
directory, never overwrites a value the user already set, and prints the
commands to start the service instead of running them.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

SERVICE_LABEL = "com.lemmedeliver.outreach"
SYSTEMD_UNIT = "insta-outreach.service"
WINDOWS_TASK = "insta-outreach"


def ensure_env_file(root: Path) -> tuple[bool, str | None]:
    """Create ``.env`` from ``.env.example`` if needed and make sure it holds a
    ``CONTROL_API_TOKEN``. Returns (created, generated token or None)."""
    env, example = root / ".env", root / ".env.example"
    created = not env.exists()
    if created:
        env.write_text(example.read_text(encoding="utf-8") if example.exists() else "", encoding="utf-8")
    lines = env.read_text(encoding="utf-8").splitlines()
    token = None
    for i, line in enumerate(lines):
        key, sep, value = line.partition("=")
        if sep and key.strip().removeprefix("export ").strip() == "CONTROL_API_TOKEN":
            if value.strip().strip("'\""):
                return created, None  # the user's own token wins
            token = secrets.token_urlsafe(32)
            lines[i] = f"CONTROL_API_TOKEN={token}"
            break
    else:
        token = secrets.token_urlsafe(32)
        lines.append(f"CONTROL_API_TOKEN={token}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return created, token


_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def set_env_values(env: Path, values: dict[str, str | None]) -> None:
    """Set (or, with None, remove) ``KEY=value`` lines in ``.env``, keeping every
    other line and comment. Values must be a single line: a newline would let a
    value smuggle in another variable."""
    for key, value in values.items():
        if not _ENV_NAME.match(key):
            raise ValueError(f"not an environment variable name: {key!r}")
        if value is not None and (not value.strip() or any(c in value for c in "\r\n\x00")):
            raise ValueError(f"{key}: the value must be one non-empty line")
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    pending = dict(values)
    kept: list[str] = []
    for line in lines:
        key, sep, _ = line.partition("=")
        name = key.strip().removeprefix("export ").strip()
        if sep and name in values:
            value = pending.pop(name, None)
            if value is not None:
                kept.append(f"{name}={value.strip()}")
            continue  # removed, or a duplicate of a line already rewritten
        kept.append(line)
    kept += [f"{name}={value.strip()}" for name, value in pending.items() if value is not None]
    temp = env.with_name(env.name + ".tmp")
    temp.write_text("\n".join(kept) + "\n", encoding="utf-8")
    os.replace(temp, env)


@dataclass
class ServicePlan:
    """A service definition to write, and the commands that start and stop it."""

    platform: str
    files: dict[Path, str] = field(default_factory=dict)
    start: list[str] = field(default_factory=list)
    stop: list[str] = field(default_factory=list)
    logs: str = ""


def service_plan(platform: str, root: Path, python: Path, home: Path) -> ServicePlan:
    """Run ``python -m insta_outreach run`` in ``root`` at log-in, restarted on failure."""
    log = root / "data" / "outreach.log"
    if platform == "darwin":
        plist = home / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
        args = ["/usr/bin/caffeinate", "-i", str(python), "-m", "insta_outreach", "run"]
        content = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n'
            f"  <key>Label</key><string>{SERVICE_LABEL}</string>\n"
            f"  <key>WorkingDirectory</key><string>{escape(str(root))}</string>\n"
            "  <key>ProgramArguments</key>\n  <array>\n"
            + "".join(f"    <string>{escape(a)}</string>\n" for a in args)
            + "  </array>\n"
            "  <key>RunAtLoad</key><true/>\n"
            "  <key>KeepAlive</key><true/>\n"
            "  <key>ThrottleInterval</key><integer>60</integer>\n"
            f"  <key>StandardOutPath</key><string>{escape(str(log))}</string>\n"
            f"  <key>StandardErrorPath</key><string>{escape(str(log))}</string>\n"
            "</dict>\n</plist>\n"
        )
        return ServicePlan(
            platform,
            {plist: content},
            start=[f'launchctl bootstrap gui/$(id -u) "{plist}"'],
            stop=[f"launchctl bootout gui/$(id -u)/{SERVICE_LABEL}"],
            logs=f'tail -f "{log}"',
        )
    if platform.startswith("linux"):
        unit = home / ".config" / "systemd" / "user" / SYSTEMD_UNIT
        content = (
            "[Unit]\nDescription=LemmeDeliver Instagram outreach\nAfter=network-online.target\n\n"
            f'[Service]\nWorkingDirectory={root}\nExecStart="{python}" -m insta_outreach run\n'
            "Restart=on-failure\nRestartSec=60\n\n[Install]\nWantedBy=default.target\n"
        )
        return ServicePlan(
            platform,
            {unit: content},
            start=[
                "systemctl --user daemon-reload",
                f"systemctl --user enable --now {SYSTEMD_UNIT.removesuffix('.service')}",
                'loginctl enable-linger "$USER"    # keep running while logged out',
            ],
            stop=[f"systemctl --user disable --now {SYSTEMD_UNIT.removesuffix('.service')}"],
            logs=f"journalctl --user -u {SYSTEMD_UNIT.removesuffix('.service')} -f",
        )
    if platform == "win32":
        launcher = root / "data" / "run-outreach.cmd"
        # UTF-8 mode: the log file is not a console, and prospects' messages can contain emoji.
        content = (
            f'@echo off\r\nset PYTHONUTF8=1\r\ncd /d "{root}"\r\n"{python}" -m insta_outreach run >> "{log}" 2>&1\r\n'
        )
        return ServicePlan(
            platform,
            {launcher: content},
            start=[f'schtasks /Create /TN "{WINDOWS_TASK}" /TR "\\"{launcher}\\"" /SC ONLOGON /RL LIMITED /F'],
            stop=[f'schtasks /End /TN "{WINDOWS_TASK}"', f'schtasks /Delete /TN "{WINDOWS_TASK}" /F'],
            logs=f'type "{log}"',
        )
    raise ValueError(f"no service template for platform {platform!r}; see docs/DEPLOY.md §4")


def current_service_plan(root: Path) -> ServicePlan:
    return service_plan(sys.platform, root.resolve(), Path(sys.executable), Path.home())


def launcher(platform: str, root: Path, python: Path) -> tuple[Path, str]:
    """A file to double-click: starts the program and opens Mission Control.
    Closing its window stops the program."""
    if platform == "win32":
        content = (
            "@echo off\r\ntitle Mission Control - keep this window open while it runs\r\n"
            f'set PYTHONUTF8=1\r\ncd /d "{root}"\r\n"{python}" -m insta_outreach run --open\r\n'
            "echo.\r\necho Mission Control has stopped. Press any key to close this window.\r\npause >nul\r\n"
        )
        return root / "Mission Control.cmd", content
    name = "Mission Control.command" if platform == "darwin" else "mission-control.sh"
    content = f'#!/bin/sh\ncd "{root}" || exit 1\nexec "{python}" -m insta_outreach run --open\n'
    return root / name, content


def write_launcher(root: Path) -> Path:
    path, content = launcher(sys.platform, root.resolve(), Path(sys.executable))
    path.write_text(content, encoding="utf-8", newline="")
    if sys.platform != "win32":
        path.chmod(0o755)
    return path
