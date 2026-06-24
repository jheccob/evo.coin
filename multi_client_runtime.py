from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import config
from database.database import db
from runtime_process import (
    clear_runtime_process_state,
    clear_runtime_stop_request,
    get_runtime_execution_log_path,
    get_runtime_process_state_path,
    get_runtime_stderr_log_path,
    get_runtime_stdout_log_path,
    get_runtime_stop_request_path,
    read_runtime_process_state,
    request_runtime_stop,
)


PROJECT_ROOT = Path(__file__).resolve().parent
BOT_RUNNER = PROJECT_ROOT / "bot_runner.py"


def _runtime_key(context: dict, *, symbol: str, timeframe: str) -> str:
    exchange_name = str(context.get("exchange_name") or context.get("exchange") or "binanceusdm").strip() or "binanceusdm"
    return f"account:{int(context['user_id'])}:{context['account_id']}:{exchange_name}:{symbol}:{timeframe}"


def _is_process_running(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    except Exception:
        return False
    return True


def _eligible_contexts(symbol: str, timeframe: str, strategy_version: str | None = None) -> list[dict]:
    return db.list_eligible_accounts_for_runtime(
        symbol=symbol,
        timeframe=timeframe,
        strategy_version=strategy_version,
    )


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
        }

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


def stop_all(*, symbol: str, timeframe: str) -> list[dict]:
    results = []
    for context in _eligible_contexts(symbol, timeframe):
        runtime_key = _runtime_key(context, symbol=symbol, timeframe=timeframe)
        process_state_path = get_runtime_process_state_path(runtime_key)
        stop_request_path = get_runtime_stop_request_path(runtime_key)
        state = read_runtime_process_state(path=process_state_path) or {}
        pid = state.get("pid")
        request_runtime_stop(path=stop_request_path)
        deadline = time.time() + 8.0
        while pid and _is_process_running(pid) and time.time() < deadline:
            time.sleep(0.5)
        results.append(
            {
                "runtime_key": runtime_key,
                "user_id": int(context["user_id"]),
                "account_id": str(context["account_id"]),
                "pid": pid,
                "status": "stopped" if not pid or not _is_process_running(pid) else "stop_requested",
            }
        )
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Gerenciador multi-cliente do Evo Coin bot.")
    parser.add_argument("action", choices=["start", "stop", "status"])
    parser.add_argument("--symbol", default=config.SYMBOL)
    parser.add_argument("--timeframe", default=config.TIMEFRAME)
    parser.add_argument("--testnet", action="store_true", help="Inicia todos em testnet.")
    parser.add_argument("--force", action="store_true", help="Ignora state file antigo quando o PID nao existe.")
    args = parser.parse_args()

    if args.action == "start":
        results = start_all(symbol=args.symbol, timeframe=args.timeframe, testnet=bool(args.testnet), force=bool(args.force))
    elif args.action == "stop":
        results = stop_all(symbol=args.symbol, timeframe=args.timeframe)
    else:
        results = status_all(symbol=args.symbol, timeframe=args.timeframe)

    for item in results:
        print(item)
    if not results:
        print("Nenhuma conta elegivel encontrada. Verifique user_accounts.live_enabled e credenciais.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
