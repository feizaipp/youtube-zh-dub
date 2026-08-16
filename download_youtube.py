#!/usr/bin/env python3
"""Download selectable-quality YouTube video and highest-quality audio streams."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


LAUNCHER = (
    Path.home()
    / ".codex"
    / "skills"
    / "youtube-zh-dub"
    / "scripts"
    / "run_youtube_zh_dub.py"
)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not LAUNCHER.is_file():
        print(f"错误：找不到 YouTube 下载启动器：{LAUNCHER}", file=sys.stderr)
        return 2
    command = [
        sys.executable,
        str(LAUNCHER),
        *arguments,
        "--download-only",
    ]
    return subprocess.run(command).returncode


if __name__ == "__main__":
    raise SystemExit(main())
