from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import config
from database.database import db
from runtime_process import (
    build_account_runtime_key,
    clear_runtime_process_state,
    clear_runtime_stop_request,
    get_runtime_execution_log_path,
    get_runtime_process_state_path,
    get_runtime_stderr_log_path,
    get_runtime_stdout_log_path,
    get_runtime_stop_request_path,
    read_runtime_process_state,
    request_runtime_stop,
    tail_text_file,
)


PROJECT_ROOT = Path(__file__).resolve().parent
BOT_RUNNER = PROJECT_ROOT / "bot_runner.py"
STOP_EVENT = threading.Event()
MANAGED_PROCESSES: dict[str, subprocess.Popen] = {}
MANAGED_PROCESS_METADATA: dict[str, dict[str, Any]] = {}
MANAGED_PROCESS_LOCK = threading.Lock()


def _runtime_key(context: dict, *, symbol: str, timeframe: str) -> str:
    exchange_name = str(context.get("exchange_name") or context.get("exchange") or "binanceusdm").strip() or "binanceusdm"
    return build_account_runtime_key(
        user_id=int(context["user_id"]),
        account_id=str(context["account_id"]),
        exchange=exchange_name,
        symbol=symbol,
        timeframe=timeframe,
    )


def _runtime_key_from_control(control: dict) -> str:
    return build_account_runtime_key(
        user_id=int(control["user_id"]),
        account_id=str(control["account_id"]),
        exchange=str(control.get("exchange") or "binanceusdm"),
        symbol=str(control.get("symbol") or config.SYMBOL),
        timeframe=str(control.get("timeframe") or config.TIMEFRAME),
    )


def _is_process_running(pid) -> bool:
    if not pid:
        return False
    try:
        resolved_pid = int(pid)
    except Exception:
        return False
    with MANAGED_PROCESS_LOCK:
        for process in MANAGED_PROCESSES.values():
            if process.pid == resolved_pid:
                return process.poll() is None
    try:
        os.kill(resolved_pid, 0)
    except OSError:
        return False
    except Exception:
        return False
    return True


def _register_managed_process(runtime_key: str, process: subprocess.Popen, metadata: dict[str, Any]) -> None:
    with MANAGED_PROCESS_LOCK:
        MANAGED_PROCESSES[runtime_key] = process
        MANAGED_PROCESS_METADATA[runtime_key] = dict(metadata)


def _drop_managed_process(runtime_key: str) -> None:
    with MANAGED_PROCESS_LOCK:
        MANAGED_PROCESSES.pop(runtime_key, None)
        MANAGED_PROCESS_METADATA.pop(runtime_key, None)


def _snapshot_managed_processes() -> list[tuple[str, subprocess.Popen, dict[str, Any]]]:
    with MANAGED_PROCESS_LOCK:
        return [
            (runtime_key, process, dict(MANAGED_PROCESS_METADATA.get(runtime_key) or {}))
            for runtime_key, process in MANAGED_PROCESSES.items()
        ]


def _record_runtime_exit_error(metadata: dict[str, Any], error_text: str) -> None:
    try:
        db.update_user_runtime_control_tracking(
            user_id=int(metadata["user_id"]),
            account_id=str(metadata["account_id"]),
            exchange=str(metadata.get("exchange") or "binanceusdm"),
            symbol=str(metadata.get("symbol") or config.SYMBOL),
            timeframe=str(metadata.get("timeframe") or config.TIMEFRAME),
            last_stopped_at=datetime.now(UTC).isoformat(),
            last_error=error_text,
        )
    except Exception as exc:
        print(f"[multi-runtime] failed to record child exit in db: {exc}", flush=True)


def reap_finished_processes() -> list[dict[str, Any]]:
    reaped: list[dict[str, Any]] = []
    for runtime_key, process, metadata in _snapshot_managed_processes():
        exit_code = process.poll()
        if exit_code is None:
            continue

        _drop_managed_process(runtime_key)
        stderr_log_path = get_runtime_stderr_log_path(runtime_key)
        stderr_tail = tail_text_file(stderr_log_path, max_lines=60, max_chars=6000)
        error_text = f"Runtime saiu com codigo {exit_code}."
        if stderr_tail:
            error_text = f"{error_text} Ultimas linhas stderr: {stderr_tail}"

        clear_runtime_process_state(path=get_runtime_process_state_path(runtime_key))
        print(
            "[multi-runtime] child process exited | "
            f"runtime_key={runtime_key} pid={process.pid} exit_code={exit_code}",
            flush=True,
        )
        if stderr_tail:
            print(f"[multi-runtime] child stderr tail | {runtime_key}\n{stderr_tail}", flush=True)

        if int(exit_code or 0) != 0:
            _record_runtime_exit_error(metadata, error_text)

        reaped.append(
            {
                "runtime_key": runtime_key,
                "pid": process.pid,
                "exit_code": exit_code,
                "status": "exited",
                "error": error_text if int(exit_code or 0) != 0 else "",
            }
        )
    return reaped


def _eligible_contexts(symbol: str, timeframe: str, strategy_version: str | None = None) -> list[dict]:
    return db.list_eligible_accounts_for_runtime(
        symbol=symbol,
        timeframe=timeframe,
        strategy_version=strategy_version,
    )


def _runtime_mode_uses_testnet(value: Any) -> bool:
    return str(value or "testnet").strip().lower() not in {"real", "live", "mainnet", "conta_real", "conta-real"}


def _state_uses_testnet(state: dict | None) -> bool:
    payload = state or {}
    if payload.get("use_testnet") is not None:
        return bool(payload.get("use_testnet"))
    mode_label = str(payload.get("mode_label") or "").strip().lower()
    return mode_label not in {"conta real", "real", "live", "mainnet"}


def _parse_iso_datetime(raw_value: Any):
    if raw_value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(raw_value))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _should_delay_restart(control: dict, cooldown_seconds: float) -> bool:
    last_start_attempt_at = _parse_iso_datetime(control.get("last_start_attempt_at"))
    if last_start_attempt_at is None:
        return False
    last_command_at = _parse_iso_datetime(control.get("last_command_at"))
    if last_command_at and last_command_at > last_start_attempt_at:
        return False
    if not str(control.get("last_error") or "").strip():
        return False
    age_seconds = (datetime.now(UTC) - last_start_attempt_at).total_seconds()
    return age_seconds < max(float(cooldown_seconds), 0.0)


def _fetch_account_record(control: dict) -> dict | None:
    account_rows = db.get_user_accounts(
        user_id=int(control["user_id"]),
        account_id=str(control["account_id"]),
        status=None,
    )
    return account_rows[0] if account_rows else None


def _is_env_single_user_control(control: dict) -> bool:
    configured_user_id = int(getattr(config, "SINGLE_USER_RUNTIME_USER_ID", 0) or 0)
    if int(control.get("user_id") or 0) != configured_user_id:
        return False
    configured_account_id = str(getattr(config, "SINGLE_USER_RUNTIME_ACCOUNT_ID", "") or "env-primary").strip() or "env-primary"
    account_id = str(control.get("account_id") or "").strip()
    valid_account_ids = {
        configured_account_id,
        f"{configured_account_id}-real",
        f"{configured_account_id}-testnet",
    }
    return account_id in valid_account_ids


def _build_env_single_user_context(control: dict) -> dict:
    exchange_name = str(
        control.get("exchange") or getattr(config, "SINGLE_USER_RUNTIME_EXCHANGE", "") or "binanceusdm"
    ).strip() or "binanceusdm"
    account_id = str(
        control.get("account_id") or getattr(config, "SINGLE_USER_RUNTIME_ACCOUNT_ID", "") or "env-primary"
    ).strip() or "env-primary"
    return {
        "user_id": int(control.get("user_id") or getattr(config, "SINGLE_USER_RUNTIME_USER_ID", 0) or 0),
        "account_id": account_id,
        "account_alias": str(getattr(config, "SINGLE_USER_RUNTIME_ACCOUNT_ALIAS", "") or account_id),
        "exchange_name": exchange_name,
        "exchange": exchange_name,
        "live_enabled": True,
        "paper_enabled": bool(config.TESTNET),
        "use_env_credentials": True,
        "credential_source": "env",
        "api_key_ref": "env",
        "reconciliation_status": "env",
        "risk_profile": {"is_valid": True, "live_enabled": True},
    }


def _account_can_run(account: dict | None) -> tuple[bool, str]:
    if not account:
        return False, "Conta nao encontrada."
    if str(account.get("status") or "").strip().lower() != "active":
        return False, "Conta inativa ou desabilitada."
    if not bool(account.get("live_enabled")):
        return False, "Conta sem live_enabled para o runtime remoto."
    return True, ""


def _start_account(context: dict, *, symbol: str, timeframe: str, testnet: bool, force: bool = False) -> dict:
    runtime_key = _runtime_key(context, symbol=symbol, timeframe=timeframe)
    process_state_path = get_runtime_process_state_path(runtime_key)
    current_state = read_runtime_process_state(path=process_state_path) or {}
    current_pid = current_state.get("pid")
    if _is_process_running(current_pid) and not force:
        return {
            "runtime_key": runtime_key,
            "user_id": int(context["user_id"]),
            "account_id": str(context["account_id"]),
            "status": "already_running",
            "pid": int(current_pid),
        }

    if force and current_pid and not _is_process_running(current_pid):
        clear_runtime_process_state(path=process_state_path)

    stdout_log_path = get_runtime_stdout_log_path(runtime_key)
    stderr_log_path = get_runtime_stderr_log_path(runtime_key)
    execution_log_path = get_runtime_execution_log_path(runtime_key)
    stop_request_path = get_runtime_stop_request_path(runtime_key)
    clear_runtime_stop_request(path=stop_request_path)

    process_env = os.environ.copy()
    process_env["TESTNET"] = "true" if bool(testnet) else "false"
    process_env["TRADER_BOT_LAUNCH_SOURCE"] = "multi_client_runtime"
    process_env["TRADER_BOT_RUNTIME_KEY"] = runtime_key
    process_env["BOT_EXECUTION_LOG_PATH"] = str(execution_log_path)
    process_env["SINGLE_USER_RUNTIME_USER_ID"] = str(int(context["user_id"]))
    process_env["SINGLE_USER_RUNTIME_ACCOUNT_ID"] = str(context["account_id"])
    process_env["SINGLE_USER_RUNTIME_ACCOUNT_ALIAS"] = str(context.get("account_alias") or context["account_id"])
    process_env["SINGLE_USER_RUNTIME_EXCHANGE"] = str(
        context.get("exchange_name") or context.get("exchange") or "binanceusdm"
    )
    process_env["SYMBOL"] = symbol
    process_env["TIMEFRAME"] = timeframe
    process_env["PYTHONUNBUFFERED"] = "1"
    use_env_credentials = bool(context.get("use_env_credentials"))
    process_env["RUNTIME_USE_ENV_CREDENTIALS"] = "1" if use_env_credentials else "0"
    process_env["RUNTIME_CREDENTIAL_SOURCE"] = "env" if use_env_credentials else "vault"

    stdout_log_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_handle = None
    stderr_handle = None
    try:
        stdout_handle = open(stdout_log_path, "a", encoding="utf-8")
        stderr_handle = open(stderr_log_path, "a", encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, str(BOT_RUNNER)],
            cwd=str(PROJECT_ROOT),
            stdout=stdout_handle,
            stderr=stderr_handle,
            env=process_env,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    finally:
        if stdout_handle is not None:
            stdout_handle.close()
        if stderr_handle is not None:
            stderr_handle.close()

    time.sleep(1.0)
    if process.poll() is not None:
        return {
            "runtime_key": runtime_key,
            "user_id": int(context["user_id"]),
            "account_id": str(context["account_id"]),
            "status": "failed",
            "pid": None,
            "stderr_log": str(stderr_log_path),
            "error": tail_text_file(stderr_log_path, max_lines=40, max_chars=4000) or "Processo encerrou logo apos o start.",
        }

    _register_managed_process(
        runtime_key,
        process,
        {
            "user_id": int(context["user_id"]),
            "account_id": str(context["account_id"]),
            "exchange": str(context.get("exchange_name") or context.get("exchange") or "binanceusdm"),
            "symbol": symbol,
            "timeframe": timeframe,
        },
    )

    return {
        "runtime_key": runtime_key,
        "user_id": int(context["user_id"]),
        "account_id": str(context["account_id"]),
        "status": "started",
        "pid": int(process.pid),
        "stdout_log": str(stdout_log_path),
        "stderr_log": str(stderr_log_path),
        "execution_log": str(execution_log_path),
    }


def start_all(*, symbol: str, timeframe: str, testnet: bool, force: bool = False) -> list[dict]:
    results = []
    for context in _eligible_contexts(symbol, timeframe):
        results.append(_start_account(context, symbol=symbol, timeframe=timeframe, testnet=testnet, force=force))
    return results


def _stop_runtime(runtime_key: str) -> dict:
    process_state_path = get_runtime_process_state_path(runtime_key)
    stop_request_path = get_runtime_stop_request_path(runtime_key)
    state = read_runtime_process_state(path=process_state_path) or {}
    pid = state.get("pid")
    request_runtime_stop(path=stop_request_path)
    deadline = time.time() + 8.0
    while pid and _is_process_running(pid) and time.time() < deadline:
        time.sleep(0.5)
    running_after_request = bool(pid and _is_process_running(pid))
    if not running_after_request:
        _drop_managed_process(runtime_key)
        clear_runtime_process_state(path=process_state_path)
        clear_runtime_stop_request(path=stop_request_path)
    return {
        "runtime_key": runtime_key,
        "pid": pid,
        "status": "stopped" if not running_after_request else "stop_requested",
    }


def stop_all(*, symbol: str, timeframe: str) -> list[dict]:
    results = []
    for context in _eligible_contexts(symbol, timeframe):
        runtime_key = _runtime_key(context, symbol=symbol, timeframe=timeframe)
        result = _stop_runtime(runtime_key)
        result.update(
            {
                "user_id": int(context["user_id"]),
                "account_id": str(context["account_id"]),
            }
        )
        results.append(result)
    return results


def status_all(*, symbol: str, timeframe: str) -> list[dict]:
    results = []
    for context in _eligible_contexts(symbol, timeframe):
        runtime_key = _runtime_key(context, symbol=symbol, timeframe=timeframe)
        state = read_runtime_process_state(path=get_runtime_process_state_path(runtime_key)) or {}
        pid = state.get("pid")
        results.append(
            {
                "runtime_key": runtime_key,
                "user_id": int(context["user_id"]),
                "account_id": str(context["account_id"]),
                "pid": pid,
                "running": bool(_is_process_running(pid)),
                "started_at": state.get("started_at"),
                "mode": state.get("mode_label"),
            }
        )
    return results


def reconcile_runtime_controls(*, retry_cooldown_seconds: float = 45.0) -> list[dict]:
    reap_finished_processes()
    controls = db.list_user_runtime_controls(limit=5000)
    results: list[dict] = []

    for control in controls:
        runtime_key = _runtime_key_from_control(control)
        process_state_path = get_runtime_process_state_path(runtime_key)
        process_state = read_runtime_process_state(path=process_state_path) or {}
        current_pid = process_state.get("pid")
        is_running = _is_process_running(current_pid)
        desired_state = str(control.get("desired_state") or "stopped").strip().lower()
        desired_testnet = _runtime_mode_uses_testnet(control.get("requested_mode"))
        now_iso = datetime.now(UTC).isoformat()

        if desired_state != "running":
            if is_running:
                stop_result = _stop_runtime(runtime_key)
                db.update_user_runtime_control_tracking(
                    user_id=int(control["user_id"]),
                    account_id=str(control["account_id"]),
                    exchange=str(control.get("exchange") or "binanceusdm"),
                    symbol=str(control.get("symbol") or config.SYMBOL),
                    timeframe=str(control.get("timeframe") or config.TIMEFRAME),
                    last_stop_requested_at=now_iso,
                    last_stopped_at=now_iso if stop_result.get("status") == "stopped" else None,
                    last_error="",
                )
                results.append(
                    {
                        **stop_result,
                        "user_id": int(control["user_id"]),
                        "account_id": str(control["account_id"]),
                        "action": "stop",
                    }
                )
            else:
                db.update_user_runtime_control_tracking(
                    user_id=int(control["user_id"]),
                    account_id=str(control["account_id"]),
                    exchange=str(control.get("exchange") or "binanceusdm"),
                    symbol=str(control.get("symbol") or config.SYMBOL),
                    timeframe=str(control.get("timeframe") or config.TIMEFRAME),
                    last_stopped_at=now_iso,
                    last_error="",
                )
                results.append(
                    {
                        "runtime_key": runtime_key,
                        "user_id": int(control["user_id"]),
                        "account_id": str(control["account_id"]),
                        "status": "idle_stopped",
                        "action": "noop",
                    }
                )
            continue

        account = _fetch_account_record(control)
        env_single_user = bool(not account and _is_env_single_user_control(control))
        can_run, account_error = (True, "") if env_single_user else _account_can_run(account)
        if not can_run:
            if is_running:
                stop_result = _stop_runtime(runtime_key)
                db.update_user_runtime_control_tracking(
                    user_id=int(control["user_id"]),
                    account_id=str(control["account_id"]),
                    exchange=str(control.get("exchange") or "binanceusdm"),
                    symbol=str(control.get("symbol") or config.SYMBOL),
                    timeframe=str(control.get("timeframe") or config.TIMEFRAME),
                    last_stop_requested_at=now_iso,
                    last_stopped_at=now_iso if stop_result.get("status") == "stopped" else None,
                    last_error=account_error,
                )
                results.append(
                    {
                        **stop_result,
                        "user_id": int(control["user_id"]),
                        "account_id": str(control["account_id"]),
                        "action": "forced_stop",
                        "error": account_error,
                    }
                )
            else:
                db.update_user_runtime_control_tracking(
                    user_id=int(control["user_id"]),
                    account_id=str(control["account_id"]),
                    exchange=str(control.get("exchange") or "binanceusdm"),
                    symbol=str(control.get("symbol") or config.SYMBOL),
                    timeframe=str(control.get("timeframe") or config.TIMEFRAME),
                    last_error=account_error,
                )
                results.append(
                    {
                        "runtime_key": runtime_key,
                        "user_id": int(control["user_id"]),
                        "account_id": str(control["account_id"]),
                        "status": "blocked",
                        "action": "noop",
                        "error": account_error,
                    }
                )
            continue

        if is_running and _state_uses_testnet(process_state) == desired_testnet:
            db.update_user_runtime_control_tracking(
                user_id=int(control["user_id"]),
                account_id=str(control["account_id"]),
                exchange=str(control.get("exchange") or "binanceusdm"),
                symbol=str(control.get("symbol") or config.SYMBOL),
                timeframe=str(control.get("timeframe") or config.TIMEFRAME),
                last_started_at=process_state.get("started_at") or now_iso,
                last_error="",
            )
            results.append(
                {
                    "runtime_key": runtime_key,
                    "user_id": int(control["user_id"]),
                    "account_id": str(control["account_id"]),
                    "pid": current_pid,
                    "status": "running",
                    "action": "noop",
                }
            )
            continue

        if is_running and _state_uses_testnet(process_state) != desired_testnet:
            stop_result = _stop_runtime(runtime_key)
            db.update_user_runtime_control_tracking(
                user_id=int(control["user_id"]),
                account_id=str(control["account_id"]),
                exchange=str(control.get("exchange") or "binanceusdm"),
                symbol=str(control.get("symbol") or config.SYMBOL),
                timeframe=str(control.get("timeframe") or config.TIMEFRAME),
                last_stop_requested_at=now_iso,
                last_stopped_at=now_iso if stop_result.get("status") == "stopped" else None,
                last_error="",
            )
            results.append(
                {
                    **stop_result,
                    "user_id": int(control["user_id"]),
                    "account_id": str(control["account_id"]),
                    "action": "restart_mode",
                }
            )
            continue

        if _should_delay_restart(control, retry_cooldown_seconds):
            results.append(
                {
                    "runtime_key": runtime_key,
                    "user_id": int(control["user_id"]),
                    "account_id": str(control["account_id"]),
                    "status": "cooldown",
                    "action": "noop",
                    "error": control.get("last_error"),
                }
            )
            continue

        if env_single_user:
            context = _build_env_single_user_context(control)
        else:
            try:
                context = db.build_account_execution_context(
                    user_id=int(control["user_id"]),
                    account_id=str(control["account_id"]),
                    exchange=str(control.get("exchange") or "binanceusdm"),
                    symbol=str(control.get("symbol") or config.SYMBOL),
                    timeframe=str(control.get("timeframe") or config.TIMEFRAME),
                )
            except Exception as exc:
                error_text = f"Falha ao montar contexto da conta: {exc}"
                db.update_user_runtime_control_tracking(
                    user_id=int(control["user_id"]),
                    account_id=str(control["account_id"]),
                    exchange=str(control.get("exchange") or "binanceusdm"),
                    symbol=str(control.get("symbol") or config.SYMBOL),
                    timeframe=str(control.get("timeframe") or config.TIMEFRAME),
                    last_error=error_text,
                )
                results.append(
                    {
                        "runtime_key": runtime_key,
                        "user_id": int(control["user_id"]),
                        "account_id": str(control["account_id"]),
                        "status": "failed_context",
                        "action": "noop",
                        "error": error_text,
                    }
                )
                continue

        db.update_user_runtime_control_tracking(
            user_id=int(control["user_id"]),
            account_id=str(control["account_id"]),
            exchange=str(control.get("exchange") or "binanceusdm"),
            symbol=str(control.get("symbol") or config.SYMBOL),
            timeframe=str(control.get("timeframe") or config.TIMEFRAME),
            last_start_attempt_at=now_iso,
        )
        start_result = _start_account(
            context,
            symbol=str(control.get("symbol") or config.SYMBOL),
            timeframe=str(control.get("timeframe") or config.TIMEFRAME),
            testnet=desired_testnet,
            force=True,
        )
        if start_result.get("status") in {"started", "already_running"}:
            db.update_user_runtime_control_tracking(
                user_id=int(control["user_id"]),
                account_id=str(control["account_id"]),
                exchange=str(control.get("exchange") or "binanceusdm"),
                symbol=str(control.get("symbol") or config.SYMBOL),
                timeframe=str(control.get("timeframe") or config.TIMEFRAME),
                last_started_at=now_iso,
                last_error="",
            )
            results.append({**start_result, "action": "start"})
        else:
            error_text = str(start_result.get("error") or "Falha ao iniciar subprocesso do runtime.")
            db.update_user_runtime_control_tracking(
                user_id=int(control["user_id"]),
                account_id=str(control["account_id"]),
                exchange=str(control.get("exchange") or "binanceusdm"),
                symbol=str(control.get("symbol") or config.SYMBOL),
                timeframe=str(control.get("timeframe") or config.TIMEFRAME),
                last_error=error_text,
            )
            results.append({**start_result, "action": "start_failed", "error": error_text})

    return results


def _handle_stop_signal(_signum, _frame) -> None:
    STOP_EVENT.set()


def run_daemon(*, poll_seconds: float = 5.0, retry_cooldown_seconds: float = 45.0) -> int:
    signal.signal(signal.SIGTERM, _handle_stop_signal)
    signal.signal(signal.SIGINT, _handle_stop_signal)
    print(
        f"[multi-runtime] daemon online | poll={poll_seconds:.1f}s | retry_cooldown={retry_cooldown_seconds:.1f}s",
        flush=True,
    )

    while not STOP_EVENT.is_set():
        try:
            results = reconcile_runtime_controls(retry_cooldown_seconds=retry_cooldown_seconds)
            meaningful_results = [
                item
                for item in results
                if str(item.get("action") or "") not in {"noop"}
                or str(item.get("status") or "") not in {"running", "idle_stopped", "cooldown"}
            ]
            for item in meaningful_results:
                print(f"[multi-runtime] {item}", flush=True)
        except Exception as exc:
            print(f"[multi-runtime] reconcile error: {exc}", flush=True)

        STOP_EVENT.wait(max(float(poll_seconds), 1.0))

    print("[multi-runtime] daemon shutdown requested", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gerenciador multi-cliente do Evo Coin bot.")
    parser.add_argument("action", choices=["start", "stop", "status", "reconcile", "daemon"])
    parser.add_argument("--symbol", default=config.SYMBOL)
    parser.add_argument("--timeframe", default=config.TIMEFRAME)
    parser.add_argument("--testnet", action="store_true", help="Inicia todos em testnet.")
    parser.add_argument("--force", action="store_true", help="Ignora state file antigo quando o PID nao existe.")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(os.getenv("MULTI_RUNTIME_POLL_SECONDS", "5") or "5"),
        help="Intervalo entre reconciliacoes do daemon.",
    )
    parser.add_argument(
        "--retry-cooldown-seconds",
        type=float,
        default=float(os.getenv("MULTI_RUNTIME_RETRY_COOLDOWN_SECONDS", "45") or "45"),
        help="Cooldown antes de repetir um start que falhou.",
    )
    args = parser.parse_args(argv)

    if args.action == "start":
        results = start_all(symbol=args.symbol, timeframe=args.timeframe, testnet=bool(args.testnet), force=bool(args.force))
    elif args.action == "stop":
        results = stop_all(symbol=args.symbol, timeframe=args.timeframe)
    elif args.action == "status":
        results = status_all(symbol=args.symbol, timeframe=args.timeframe)
    elif args.action == "reconcile":
        results = reconcile_runtime_controls(retry_cooldown_seconds=float(args.retry_cooldown_seconds))
    else:
        return run_daemon(
            poll_seconds=float(args.poll_seconds),
            retry_cooldown_seconds=float(args.retry_cooldown_seconds),
        )

    for item in results:
        print(item)
    if not results:
        print("Nenhuma conta elegivel/controle encontrado. Verifique contas, credenciais e runtime controls.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
