from __future__ import annotations

import os
import runpy
import subprocess
import sys
import signal
import traceback
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
    os.environ["TRADER_BOT_EMBEDDED"] = "1"
    print(f"[railway] starting bot process from: {bot_entrypoint}", flush=True)
    bot_process = subprocess.Popen(
        [sys.executable, str(bot_entrypoint)],
        cwd=str(repo_root),
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
    port = str(os.getenv("PORT", "8501")).strip() or "8501"
    print(
        f"[railway] booting mode={mode} port={port} python={sys.version.split()[0]} cwd={repo_root}",
        flush=True,
    )

    if mode in {"all", "both", "full"}:
        return _run_all(repo_root)
    if mode in {"bot", "trader", "telegram"}:
        return _run_bot(repo_root)
    return _run_dashboard(repo_root)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        print("[railway] fatal error on startup:", flush=True)
        traceback.print_exc()
        raise
