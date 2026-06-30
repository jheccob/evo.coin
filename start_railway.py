from __future__ import annotations

import os
import runpy
import subprocess
import sys
import signal
import traceback
from pathlib import Path


DEFAULT_LOCAL_PORT = "8080"
LOCAL_VENV_REEXEC_ENV = "EVO_LOCAL_VENV_REEXEC"


def _resolve_service_port() -> str:
    return str(os.getenv("PORT", DEFAULT_LOCAL_PORT)).strip() or DEFAULT_LOCAL_PORT


def _resolve_project_venv_python(repo_root: Path) -> Path | None:
    candidates = [
        repo_root / ".venv" / "Scripts" / "python.exe",
        repo_root / ".venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return None


def _maybe_run_in_local_venv(repo_root: Path) -> int | None:
    if str(os.getenv(LOCAL_VENV_REEXEC_ENV, "")).strip() == "1":
        return None
    project_venv_python = _resolve_project_venv_python(repo_root)
    if project_venv_python is None:
        return None

    current_python = Path(sys.executable).resolve()
    if current_python == project_venv_python:
        return None

    child_env = os.environ.copy()
    child_env[LOCAL_VENV_REEXEC_ENV] = "1"
    child_env.setdefault("PORT", DEFAULT_LOCAL_PORT)
    cmd = [str(project_venv_python), *sys.argv]
    print(
        f"[railway] local re-exec via project venv python={project_venv_python}",
        flush=True,
    )
    return subprocess.call(cmd, cwd=str(repo_root), env=child_env)


def _get_bot_entrypoint(repo_root: Path) -> Path:
    return repo_root / "bot_runner.py"


def _run_dashboard(repo_root: Path) -> int:
    port = _resolve_service_port()
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
        "--server.enableCORS",
        "false",
        "--server.enableXsrfProtection",
        "false",
        "--browser.gatherUsageStats",
        "false",
    ]
    return subprocess.call(cmd, cwd=str(repo_root))


def _run_bot(repo_root: Path) -> int:
    multiuser_runtime_enabled = str(os.getenv("ENABLE_MULTIUSER_RUNTIME", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if multiuser_runtime_enabled:
        cmd = [sys.executable, str(repo_root / "multi_client_runtime.py"), "daemon"]
        return subprocess.call(cmd, cwd=str(repo_root))
    runpy.run_path(str(_get_bot_entrypoint(repo_root)), run_name="__main__")
    return 0


def _run_all(repo_root: Path) -> int:
    """
    Executa bot e dashboard no mesmo container.
    Mantem o dashboard no foreground e finaliza o bot no shutdown.
    """
    bot_entrypoint = _get_bot_entrypoint(repo_root)
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
    local_venv_exit_code = _maybe_run_in_local_venv(repo_root)
    if local_venv_exit_code is not None:
        return int(local_venv_exit_code)
    mode = str(os.getenv("RAILWAY_SERVICE_MODE", "dashboard")).strip().lower()
    port = _resolve_service_port()
    print(
        f"[railway] booting mode={mode} port={port} python={sys.version.split()[0]} cwd={repo_root}",
        flush=True,
    )

    if mode in {"all", "both", "full"}:
        return _run_all(repo_root)
    if mode in {"bot", "trader", "telegram", "runner", "bot_runner"}:
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
