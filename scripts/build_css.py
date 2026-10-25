"""Compile the web interface stylesheet with the Tailwind standalone CLI.

Usage (from the project root):

    python -m scripts.build_css                    # minified build
    python -m scripts.build_css --watch            # rebuild on template changes
    python -m scripts.build_css --tailwind PATH    # use a specific executable

The compiled ``app/static/css/app.css`` is committed, so this is only needed
after changing templates or ``app/static/src/app.css``. No Node.js is used:
the Tailwind standalone executable (v4.3.3) is taken from ``--tailwind`` or
looked up in ``tools/tailwind/`` (git-ignored). It reads no environment
variables and never downloads anything; see the README for where to get the
executable.
"""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "app" / "static" / "src" / "app.css"
OUTPUT = ROOT / "app" / "static" / "css" / "app.css"
TOOLS = ROOT / "tools" / "tailwind"


def _executable(configured: str | None) -> Path | None:
    if configured:
        return Path(configured)
    system = platform.system().lower()
    machine = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
    names = {
        "windows": [f"tailwindcss-windows-{machine}.exe"],
        "darwin": [f"tailwindcss-macos-{machine}"],
        "linux": [f"tailwindcss-linux-{machine}"],
    }.get(system, [])
    for name in [*names, "tailwindcss.exe", "tailwindcss"]:
        candidate = TOOLS / name
        if candidate.is_file():
            return candidate
    return None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Compile app/static/css/app.css.")
    parser.add_argument("--tailwind", help="Path to the Tailwind standalone executable.")
    parser.add_argument("--watch", action="store_true", help="Rebuild on changes.")
    args = parser.parse_args(argv)
    binary = _executable(args.tailwind)
    if binary is None or not binary.is_file():
        print(
            "Tailwind standalone CLI not found. Put it in tools/tailwind/ or pass "
            "--tailwind PATH (see README, 'Rebuilding the stylesheet').",
            file=sys.stderr,
        )
        return 1
    command = [str(binary), "--input", str(SOURCE), "--output", str(OUTPUT)]
    command.append("--watch" if args.watch else "--minify")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
