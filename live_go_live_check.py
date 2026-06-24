from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import config

PROJECT_ROOT = Path(__file__).resolve().parent
BACKTEST_DIR = PROJECT_ROOT / "reports" / "backtests"
DB_PATH = PROJECT_ROOT / "data" / "trading_bot.db"


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str
    action: str = ""


def _parse_iso_datetime(raw_value: Any) -> datetime | None:
    if raw_value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
    except Exception:
        return None


def _latest_backtest_report(symbol: str = "BTC/USDT", timeframe: str = "15m", days: int = 364) -> Path | None:
    pattern = f"backtest_{symbol.replace('/', '_')}_{timeframe}_{days}d_*.json"
    candidates = sorted(BACKTEST_DIR.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _load_backtest_summary(report_path: Path | None) -> dict[str, Any] | None:
    if report_path is None or not report_path.exists():
        return None
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    summary = payload.get("summary")
    return summary if isinstance(summary, dict) else None


def evaluate_backtest_gate(summary: dict[str, Any] | None) -> CheckResult:
    if not summary:
        return CheckResult(
            name="Backtest Baseline",
            status="FAIL",
            detail="Nenhum summary de backtest valido encontrado para BTC/USDT 15m.",
            action="Gerar um backtest anual atualizado antes de pensar em conta real.",
        )

    trades = int(summary.get("trades", 0) or 0)
    profit_factor = float(summary.get("profit_factor", 0.0) or 0.0)
    max_drawdown = float(summary.get("max_drawdown", 0.0) or 0.0)
    net_pct = float(summary.get("net_pct", 0.0) or 0.0)
    long_net = float(((summary.get("long_stats") or {}).get("net", 0.0)) or 0.0)
    short_net = float(((summary.get("short_stats") or {}).get("net", 0.0)) or 0.0)

    detail = (
        f"trades={trades} | pf={profit_factor:.2f} | dd={max_drawdown:.2f}% | "
        f"net={net_pct:.2f}% | long_net={long_net:.2f}% | short_net={short_net:.2f}%"
    )

    if (
        trades >= 100
        and profit_factor >= 1.20
        and max_drawdown <= 10.0
        and net_pct > 0.0
        and long_net > 0.0
        and short_net > 0.0
    ):
        return CheckResult("Backtest Baseline", "PASS", detail)

    if net_pct > 0.0 and profit_factor >= 1.0:
        return CheckResult(
            "Backtest Baseline",
            "WARN",
            detail,
            "O baseline ainda esta positivo, mas nao atingiu a regua conservadora completa.",
        )

    return CheckResult(
        "Backtest Baseline",
        "FAIL",
        detail,
        "Nao liberar conta real enquanto o baseline nao recuperar edge suficiente.",
    )


def evaluate_risk_alignment() -> list[CheckResult]:
    checks: list[CheckResult] = []

    risk_per_trade = float(config.RISK_PER_TRADE_PCT or 0.0)
    max_risk_start = float(config.MAX_REAL_RISK_PER_TRADE_PCT_START or 0.25)
    daily_real_loss = float(config.MAX_DAILY_REAL_LOSS_PCT or 0.0)
    consecutive_real_losses = int(config.MAX_CONSECUTIVE_REAL_LOSSES or 0)
    max_open_trades = int(config.MAX_OPEN_TRADES or 0)
    max_open_real_trades = int(config.MAX_OPEN_REAL_TRADES or 0)

    checks.append(
        CheckResult(
            "Risco por Trade",
            "PASS" if risk_per_trade <= max_risk_start else "FAIL",
            f"risk_per_trade={risk_per_trade:.2f}% | live_cap={max_risk_start:.2f}%",
            "" if risk_per_trade <= max_risk_start else "Baixar RISK_PER_TRADE_PCT antes de armar o live.",
        )
    )
    checks.append(
        CheckResult(
            "Loss Diario",
            "PASS" if daily_real_loss <= 2.0 else "WARN",
            f"max_daily_real_loss_pct={daily_real_loss:.2f}%",
            "" if daily_real_loss <= 2.0 else "Para piloto conservador, prefira 2.0% ou menos.",
        )
    )
    checks.append(
        CheckResult(
            "Streak de Perda",
            "PASS" if consecutive_real_losses <= 3 else "WARN",
            f"max_consecutive_real_losses={consecutive_real_losses}",
            "" if consecutive_real_losses <= 3 else "Para piloto conservador, prefira 3 losses seguidos ou menos.",
        )
    )
    checks.append(
        CheckResult(
            "Exposicao Simultanea",
            "PASS" if max_open_trades <= 1 and max_open_real_trades <= 1 else "WARN",
            f"max_open_trades={max_open_trades} | max_open_real_trades={max_open_real_trades}",
            "" if max_open_trades <= 1 and max_open_real_trades <= 1 else "Reduzir a exposicao simultanea para 1 posicao.",
        )
    )

    return checks


def evaluate_runtime_mode() -> list[CheckResult]:
    live_enabled = bool(config.ProductionConfig.ENABLE_LIVE_EXECUTION)
    confirmation = str(config.LIVE_TRADING_CONFIRMATION or "").strip().upper()
    api_key = os.getenv("BINANCE_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_SECRET_KEY", "").strip()
    credential_source = "env"

    if not api_key or not api_secret:
        try:
            from database.database import db
            from services.credential_vault import CredentialVault

            vault = CredentialVault(strict=False)
            if vault.is_configured():
                base_account_id = str(getattr(config, "SINGLE_USER_RUNTIME_ACCOUNT_ID", "") or "env-primary").strip() or "env-primary"
                exchange_name = str(getattr(config, "SINGLE_USER_RUNTIME_EXCHANGE", "") or "binanceusdm").strip() or "binanceusdm"
                user_id = int(getattr(config, "SINGLE_USER_RUNTIME_USER_ID", 0) or 0)
                credentials = vault.load_exchange_credentials(
                    db,
                    user_id=user_id,
                    account_id=f"{base_account_id}-real",
                    exchange=exchange_name,
                )
                api_key = str(credentials.get("api_key") or "").strip()
                api_secret = str(credentials.get("api_secret") or "").strip()
                if api_key and api_secret:
                    credential_source = "vault"
        except Exception:
            pass

    checks = [
        CheckResult(
            "Modo Seguro Atual",
            "PASS" if bool(config.TESTNET) and not live_enabled else "WARN",
            f"testnet={bool(config.TESTNET)} | live_enabled={live_enabled}",
            "Enquanto nao houver decisao de go-live, o recomendado e continuar em TESTNET com live desligado.",
        ),
        CheckResult(
            "Armar Live",
            "INFO" if not live_enabled else "PASS",
            f"enable_live_execution={live_enabled}",
            "So mudar para True quando a conta real for ser ativada de fato.",
        ),
        CheckResult(
            "Confirmacao Real",
            "INFO" if confirmation != "EU_ASSUMO_RISCO" else "PASS",
            f"live_trading_confirmation={'set' if confirmation else 'empty'}",
            "Preencher apenas no momento exato da virada para conta real.",
        ),
        CheckResult(
            "Credenciais de Exchange",
            "PASS" if api_key and api_secret else "WARN",
            (
                f"api_key={'ok' if api_key else 'missing'} | "
                f"api_secret={'ok' if api_secret else 'missing'} | "
                f"source={credential_source if api_key and api_secret else 'missing'}"
            ),
            "Configurar credenciais reais via env ou vault antes do primeiro piloto real.",
        ),
    ]
    return checks


def evaluate_runtime_db_state(symbol: str = "BTC/USDT", timeframe: str = "15m") -> CheckResult:
    if not DB_PATH.exists():
        return CheckResult(
            "Runtime DB",
            "WARN",
            "Banco local ainda nao encontrado.",
            "Subir o runtime ao menos em TESTNET para gerar heartbeat e snapshot.",
        )

    runtime_key = f"primary:{symbol}:{timeframe}"
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT runtime_key, environment, status, last_heartbeat_at, last_error
            FROM bot_runtime_state
            WHERE runtime_key = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (runtime_key,),
        )
        row = cursor.fetchone()
    finally:
        conn.close()

    if not row:
        return CheckResult(
            "Runtime DB",
            "WARN",
            f"Sem snapshot persistido para {runtime_key}.",
            "Executar o bot em TESTNET para confirmar heartbeat, status e reconciliação.",
        )

    payload = dict(row)
    heartbeat_at = _parse_iso_datetime(payload.get("last_heartbeat_at"))
    heartbeat_age = None
    if heartbeat_at is not None:
        heartbeat_age = max((datetime.now(UTC) - heartbeat_at.astimezone(UTC)).total_seconds(), 0.0)
    detail = (
        f"environment={payload.get('environment')} | status={payload.get('status')} | "
        f"heartbeat_age_sec={int(heartbeat_age) if heartbeat_age is not None else 'n/a'} | "
        f"last_error={payload.get('last_error') or '-'}"
    )

    if payload.get("last_error"):
        return CheckResult("Runtime DB", "WARN", detail, "Resolver erros persistidos antes de pensar em go-live.")
    if str(payload.get("environment") or "").strip().lower() == "testnet":
        return CheckResult("Runtime DB", "PASS", detail)
    return CheckResult("Runtime DB", "WARN", detail, "Confirmar que o runtime de staging continua separado do real.")


def build_go_live_report() -> dict[str, Any]:
    report_path = _latest_backtest_report()
    summary = _load_backtest_summary(report_path)

    checks: list[CheckResult] = []
    checks.extend(evaluate_runtime_mode())
    checks.extend(evaluate_risk_alignment())
    checks.append(evaluate_backtest_gate(summary))
    checks.append(evaluate_runtime_db_state())

    critical_fail = any(item.status == "FAIL" for item in checks)
    structure_aligned = not critical_fail
    live_armed = bool(config.ProductionConfig.ENABLE_LIVE_EXECUTION) and str(config.LIVE_TRADING_CONFIRMATION or "").strip().upper() == "EU_ASSUMO_RISCO"

    return {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "symbol": config.SYMBOL,
        "timeframe": config.TIMEFRAME,
        "latest_backtest_report": None if report_path is None else str(report_path),
        "structure_aligned_for_conservative_live": structure_aligned,
        "live_mode_armed": live_armed,
        "checks": [item.__dict__ for item in checks],
    }


def print_go_live_report(report: dict[str, Any]) -> None:
    print("EVO COIN | GO-LIVE CHECK")
    print(f"generated_at_utc: {report['generated_at_utc']}")
    print(f"symbol: {report['symbol']} | timeframe: {report['timeframe']}")
    print(f"latest_backtest_report: {report.get('latest_backtest_report') or '-'}")
    print(
        "structure_aligned_for_conservative_live: "
        f"{'YES' if report['structure_aligned_for_conservative_live'] else 'NO'}"
    )
    print(f"live_mode_armed: {'YES' if report['live_mode_armed'] else 'NO'}")
    print("")
    for item in report["checks"]:
        print(f"[{item['status']}] {item['name']}: {item['detail']}")
        if item.get("action"):
            print(f"  action: {item['action']}")


def main() -> None:
    report = build_go_live_report()
    print_go_live_report(report)


if __name__ == "__main__":
    main()
