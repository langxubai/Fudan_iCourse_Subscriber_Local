"""Print a daily macOS LaunchAgent definition for the local iCourse sync."""

import os
import plistlib
import shutil
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent.parent
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        sys.exit("找不到 ffmpeg。请先按 README 完成本地安装。")

    log = root / "data" / "scheduled-sync.log"
    job = {
        "Label": "local.fudan.icourse.sync",
        "ProgramArguments": [
            str(root / ".venv" / "bin" / "python"),
            "-u",
            str(root / "local_app.py"),
            "sync",
        ],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {"PATH": str(Path(ffmpeg).parent) + ":" + os.defpath},
        "StartCalendarInterval": {"Hour": 19, "Minute": 36},
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "Umask": 0o077,
    }
    plistlib.dump(job, sys.stdout.buffer)


if __name__ == "__main__":
    main()
