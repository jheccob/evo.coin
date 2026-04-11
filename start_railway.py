from __future__ import annotations

import os
import runpy
import subprocess
import sys
import signal
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


def _run_all(repo_root: Path) -> int:
    """
    Executa bot e dashboard no mesmo container.
    Mantem o dashboard no foreground e finaliza o bot no shutdown.
    """
    bot_entrypoint = repo_root / "start_telegram_bot.py"
    bot_process = subprocess.Popen(
        [sys.executable, str(bot_entrypoint)],
        cwd=str(repo_root),
        stdout=sys.stdout,
        stderr=sys.stderr,
    )

    def _terminate_bot(*_args):
        if bot_process.poll() is None:
            try:
                bot_process.terminate()
                bot_process.wait(timeout=15)
            except Exception:
                try:
                    bot_process.kill()
                except Exception:
                    pass

    signal.signal(signal.SIGTERM, _terminate_bot)
    signal.signal(signal.SIGINT, _terminate_bot)

    dashboard_exit = _run_dashboard(repo_root)
    _terminate_bot()
    return dashboard_exit


def main() -> int:
    repo_root = Path(__file__).resolve().parent
    mode = str(os.getenv("RAILWAY_SERVICE_MODE", "dashboard")).strip().lower()

    if mode in {"all", "both", "full"}:
        return _run_all(repo_root)
    if mode in {"bot", "trader", "telegram"}:
        return _run_bot(repo_root)
    return _run_dashboard(repo_root)


if __name__ == "__main__":
    raise SystemExit(main())
