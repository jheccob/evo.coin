from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
LOGS_DIR = PROJECT_ROOT / "logs"
RUNTIME_PROCESS_STATE_PATH = LOGS_DIR / "trader_bot_process.json"
RUNTIME_STOP_REQUEST_PATH = LOGS_DIR / "trader_bot_stop.signal"
BOT_EXECUTION_LOG_PATH = LOGS_DIR / "bot_execution.log"
BOT_RUNNER_STDOUT_LOG_PATH = LOGS_DIR / "bot_runner_stdout.log"
BOT_RUNNER_STDERR_LOG_PATH = LOGS_DIR / "bot_runner_stderr.log"


def runtime_key_to_slug(runtime_key: str | None) -> str:
    raw_value = str(runtime_key or "primary").strip() or "primary"
    safe_chars = []
    for char in raw_value:
        if char.isalnum() or char in {"-", "_"}:
            safe_chars.append(char)
        else:
            safe_chars.append("_")
    slug = "".join(safe_chars).strip("_")
    return slug[:120] or "primary"


def get_runtime_process_state_path(runtime_key: str | None = None) -> Path:
    if not runtime_key:
        return RUNTIME_PROCESS_STATE_PATH
    return LOGS_DIR / f"trader_bot_process_{runtime_key_to_slug(runtime_key)}.json"


def get_runtime_stop_request_path(runtime_key: str | None = None) -> Path:
    if not runtime_key:
        return RUNTIME_STOP_REQUEST_PATH
    return LOGS_DIR / f"trader_bot_stop_{runtime_key_to_slug(runtime_key)}.signal"


def get_runtime_stdout_log_path(runtime_key: str | None = None) -> Path:
    if not runtime_key:
        return BOT_RUNNER_STDOUT_LOG_PATH
    return LOGS_DIR / f"bot_runner_stdout_{runtime_key_to_slug(runtime_key)}.log"


def get_runtime_stderr_log_path(runtime_key: str | None = None) -> Path:
    if not runtime_key:
        return BOT_RUNNER_STDERR_LOG_PATH
    return LOGS_DIR / f"bot_runner_stderr_{runtime_key_to_slug(runtime_key)}.log"


def get_runtime_execution_log_path(runtime_key: str | None = None) -> Path:
    if not runtime_key:
        return BOT_EXECUTION_LOG_PATH
    return LOGS_DIR / f"bot_execution_{runtime_key_to_slug(runtime_key)}.log"


def ensure_runtime_logs_dir(path: Path | None = None) -> Path:
    target_dir = Path(path or LOGS_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


def _resolve_path(path: Path | None, default_path: Path) -> Path:
    resolved_path = Path(path or default_path)
    ensure_runtime_logs_dir(resolved_path.parent)
    return resolved_path


def read_runtime_process_state(path: Path | None = None) -> dict[str, Any] | None:
    target_path = _resolve_path(path, RUNTIME_PROCESS_STATE_PATH)
    if not target_path.exists():
        return None
    try:
        payload = json.loads(target_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def write_runtime_process_state(
    *,
    pid: int,
    use_testnet: bool,
    entrypoint: str,
    source: str,
    command: str | None = None,
    path: Path | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target_path = _resolve_path(path, RUNTIME_PROCESS_STATE_PATH)
    payload: dict[str, Any] = {
        "pid": int(pid),
        "use_testnet": bool(use_testnet),
        "mode_label": "Testnet" if bool(use_testnet) else "Conta Real",
        "entrypoint": str(entrypoint),
        "source": str(source or "").strip() or "unknown",
        "command": str(command or "").strip(),
        "started_at": datetime.now(UTC).isoformat(),
    }
    if extra:
        payload.update(extra)
    target_path.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True), encoding="utf-8")
    return payload


def clear_runtime_process_state(path: Path | None = None) -> None:
    target_path = _resolve_path(path, RUNTIME_PROCESS_STATE_PATH)
    try:
        target_path.unlink(missing_ok=True)
    except Exception:
        return


def request_runtime_stop(path: Path | None = None) -> Path:
    target_path = _resolve_path(path, RUNTIME_STOP_REQUEST_PATH)
    target_path.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
    return target_path


def runtime_stop_requested(path: Path | None = None) -> bool:
    target_path = _resolve_path(path, RUNTIME_STOP_REQUEST_PATH)
    return target_path.exists()


def clear_runtime_stop_request(path: Path | None = None) -> None:
    target_path = _resolve_path(path, RUNTIME_STOP_REQUEST_PATH)
    try:
        target_path.unlink(missing_ok=True)
    except Exception:
        return


def tail_text_file(path: Path | str, *, max_lines: int = 120, max_chars: int = 30000) -> str:
    target_path = Path(path)
    if not target_path.exists():
        return ""
    try:
        content = target_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = content.splitlines()
    tail_lines = lines[-max(1, int(max_lines)) :]
    tail_text = "\n".join(tail_lines)
    if len(tail_text) <= max_chars:
        return tail_text
    return tail_text[-max_chars:]
