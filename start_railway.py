from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path


def _run_dashboard(repo_root: Path) -> int:
    port = str(os.getenv("PORT", "8501")).strip() or "8501"
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(repo_root / "app.py"),
        "--server.address",
        "0.0.0.0",
        "--server.port",
        port,
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    return subprocess.call(cmd, cwd=str(repo_root))


def _run_bot(repo_root: Path) -> int:
    runpy.run_path(str(repo_root / "start_telegram_bot.py"), run_name="__main__")
    return 0


def main() -> int:
    repo_root = Path(__file__).resolve().parent
    mode = str(os.getenv("RAILWAY_SERVICE_MODE", "dashboard")).strip().lower()

    if mode in {"bot", "trader", "telegram"}:
        return _run_bot(repo_root)
    return _run_dashboard(repo_root)


if __name__ == "__main__":
    raise SystemExit(main())
