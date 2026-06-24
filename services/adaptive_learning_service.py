from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


@dataclass
class LearningBias:
    bias: float
    trade_count: int
    win_rate_pct: float
    avg_net_pct: float


@dataclass
class SetupGuard:
    blocked: bool
    cooldown_remaining: int
    consecutive_losses: int
    recent_trade_count: int
    recent_profit_factor: float
    recent_avg_net_pct: float
    reason: str


class AdaptiveLearningService:
    def __init__(
        self,
        path: str | Path,
        *,
        enabled: bool = True,
        min_trades: int = 6,
        max_bias: float = 0.12,
    ):
        self.path = Path(path)
        self.enabled = bool(enabled)
        self.min_trades = max(int(min_trades), 1)
        self.max_bias = max(float(max_bias), 0.0)
        self.state = {
            "updated_at_utc": None,
            "stats": {},
        }
        self._load()

    def _load(self) -> None:
        if not self.enabled or not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                self.state = {
                    "updated_at_utc": payload.get("updated_at_utc"),
                    "stats": payload.get("stats") or {},
                }
        except Exception:
            self.state = {
                "updated_at_utc": None,
                "stats": {},
            }

    def save(self) -> None:
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state["updated_at_utc"] = _utc_now()
        self.path.write_text(json.dumps(self.state, ensure_ascii=True, indent=2), encoding="utf-8")

    def reset(self) -> None:
        self.state = {"updated_at_utc": None, "stats": {}}
        if self.path.exists():
            try:
                self.path.unlink()
            except Exception:
                pass

    def _build_keys(self, *, symbol: str, timeframe: str, side: str, setup_name: str) -> list[str]:
        compact_symbol = str(symbol or "").strip().upper()
        compact_timeframe = str(timeframe or "").strip().lower()
        compact_side = str(side or "").strip().lower()
        compact_setup = str(setup_name or "unknown").strip().lower() or "unknown"
        return [
            f"{compact_symbol}|{compact_timeframe}|{compact_side}|{compact_setup}",
            f"{compact_symbol}|{compact_timeframe}|{compact_side}|__all__",
            f"{compact_symbol}|__all__|{compact_side}|__all__",
        ]

    def _resolve_guard_settings(self) -> dict[str, float | int | bool]:
        import config  # local import to avoid circular import at module load

        return {
            "enabled": bool(getattr(config.ProductionConfig, "AI_SETUP_GUARD_ENABLED", False)),
            "min_trades": max(int(getattr(config.ProductionConfig, "AI_SETUP_GUARD_MIN_TRADES", 8) or 8), 1),
            "lookback": max(int(getattr(config.ProductionConfig, "AI_SETUP_GUARD_LOOKBACK", 10) or 10), 1),
            "max_consecutive_losses": max(
                int(getattr(config.ProductionConfig, "AI_SETUP_GUARD_MAX_CONSECUTIVE_LOSSES", 3) or 3),
                1,
            ),
            "cooldown_signals": max(int(getattr(config.ProductionConfig, "AI_SETUP_GUARD_COOLDOWN_SIGNALS", 3) or 3), 1),
            "min_recent_pf": max(float(getattr(config.ProductionConfig, "AI_SETUP_GUARD_MIN_RECENT_PF", 0.95) or 0.0), 0.0),
            "max_recent_avg_net_pct": float(
                getattr(config.ProductionConfig, "AI_SETUP_GUARD_MAX_RECENT_AVG_NET_PCT", -0.05) or 0.0
            ),
        }

    @staticmethod
    def _recent_profit_factor(recent_net_pcts: list[float]) -> float:
        wins = sum(value for value in recent_net_pcts if value > 0)
        losses = sum(abs(value) for value in recent_net_pcts if value < 0)
        if losses <= 0:
            return 99.0 if wins > 0 else 0.0
        return wins / losses

    def register_trade(
        self,
        *,
        symbol: str,
        timeframe: str,
        side: str,
        setup_name: str,
        net_pct: float,
    ) -> None:
        if not self.enabled:
            return
        keys = self._build_keys(
            symbol=symbol,
            timeframe=timeframe,
            side=side,
            setup_name=setup_name,
        )
        resolved_net_pct = float(net_pct or 0.0)
        guard_settings = self._resolve_guard_settings()
        for index, key in enumerate(keys):
            stats = dict((self.state.get("stats") or {}).get(key) or {})
            trades = int(stats.get("trades", 0) or 0) + 1
            wins = int(stats.get("wins", 0) or 0) + (1 if resolved_net_pct > 0 else 0)
            losses = int(stats.get("losses", 0) or 0) + (1 if resolved_net_pct <= 0 else 0)
            net_sum = float(stats.get("net_sum_pct", 0.0) or 0.0) + resolved_net_pct
            if index == 0:
                recent_net_pcts = list(stats.get("recent_net_pcts") or [])
                recent_net_pcts.append(resolved_net_pct)
                recent_net_pcts = recent_net_pcts[-int(guard_settings["lookback"]):]
                consecutive_losses = 0 if resolved_net_pct > 0 else int(stats.get("consecutive_losses", 0) or 0) + 1
                cooldown_remaining = int(stats.get("cooldown_remaining", 0) or 0)
                recent_avg_net_pct = sum(recent_net_pcts) / len(recent_net_pcts)
                recent_profit_factor = self._recent_profit_factor(recent_net_pcts)
                if bool(guard_settings["enabled"]):
                    if consecutive_losses >= int(guard_settings["max_consecutive_losses"]):
                        cooldown_remaining = int(guard_settings["cooldown_signals"])
                    elif (
                        len(recent_net_pcts) >= int(guard_settings["min_trades"])
                        and recent_profit_factor < float(guard_settings["min_recent_pf"])
                        and recent_avg_net_pct <= float(guard_settings["max_recent_avg_net_pct"])
                    ):
                        cooldown_remaining = max(cooldown_remaining, int(guard_settings["cooldown_signals"]) - 1)
                stats.update(
                    {
                        "recent_net_pcts": [round(float(value), 6) for value in recent_net_pcts],
                        "consecutive_losses": int(consecutive_losses),
                        "cooldown_remaining": int(cooldown_remaining),
                        "recent_profit_factor": round(float(recent_profit_factor), 6),
                        "recent_avg_net_pct": round(float(recent_avg_net_pct), 6),
                    }
                )
            stats.update(
                {
                    "trades": trades,
                    "wins": wins,
                    "losses": losses,
                    "net_sum_pct": round(net_sum, 6),
                    "avg_net_pct": round(net_sum / max(trades, 1), 6),
                    "win_rate_pct": round((wins / max(trades, 1)) * 100.0, 4),
                    "updated_at_utc": _utc_now(),
                }
            )
            self.state.setdefault("stats", {})[key] = stats
        self.save()

    def register_trade_payload(self, *, symbol: str, timeframe: str, trade: dict) -> None:
        side = str(trade.get("side") or "").strip().lower()
        if side not in {"long", "short"}:
            return
        setup_name = str(trade.get("entry_setup") or trade.get("setup_name") or "unknown")
        net_pct = trade.get("net_pct")
        if net_pct is None:
            net_pct = trade.get("gross_pct", 0.0)
        self.register_trade(
            symbol=symbol,
            timeframe=timeframe,
            side=side,
            setup_name=setup_name,
            net_pct=_safe_float(net_pct, 0.0),
        )

    def get_bias(self, *, symbol: str, timeframe: str, side: str, setup_name: str) -> LearningBias:
        if not self.enabled:
            return LearningBias(bias=0.0, trade_count=0, win_rate_pct=0.0, avg_net_pct=0.0)
        keys = self._build_keys(
            symbol=symbol,
            timeframe=timeframe,
            side=side,
            setup_name=setup_name,
        )
        weighted_bias = 0.0
        weighted_trades = 0.0
        best_trade_count = 0
        best_win_rate = 0.0
        best_avg_net = 0.0

        for weight, key in zip((1.0, 0.55, 0.25), keys):
            stats = dict((self.state.get("stats") or {}).get(key) or {})
            trade_count = int(stats.get("trades", 0) or 0)
            if trade_count < self.min_trades:
                continue
            win_rate = float(stats.get("win_rate_pct", 0.0) or 0.0)
            avg_net_pct = float(stats.get("avg_net_pct", 0.0) or 0.0)
            win_component = (win_rate - 50.0) / 50.0
            expectancy_component = avg_net_pct / 2.0
            raw_bias = (win_component * 0.08) + (expectancy_component * 0.04)
            clipped = max(-self.max_bias, min(self.max_bias, raw_bias))
            weighted_bias += clipped * weight
            weighted_trades += weight
            if trade_count > best_trade_count:
                best_trade_count = trade_count
                best_win_rate = win_rate
                best_avg_net = avg_net_pct

        if weighted_trades <= 0:
            return LearningBias(bias=0.0, trade_count=0, win_rate_pct=0.0, avg_net_pct=0.0)
        return LearningBias(
            bias=round(weighted_bias / weighted_trades, 6),
            trade_count=best_trade_count,
            win_rate_pct=round(best_win_rate, 4),
            avg_net_pct=round(best_avg_net, 6),
        )

    def get_setup_guard(self, *, symbol: str, timeframe: str, side: str, setup_name: str) -> SetupGuard:
        settings = self._resolve_guard_settings()
        if not self.enabled or not bool(settings["enabled"]):
            return SetupGuard(False, 0, 0, 0, 0.0, 0.0, "")
        exact_key = self._build_keys(symbol=symbol, timeframe=timeframe, side=side, setup_name=setup_name)[0]
        stats = dict((self.state.get("stats") or {}).get(exact_key) or {})
        cooldown_remaining = int(stats.get("cooldown_remaining", 0) or 0)
        consecutive_losses = int(stats.get("consecutive_losses", 0) or 0)
        recent_net_pcts = list(stats.get("recent_net_pcts") or [])
        recent_trade_count = len(recent_net_pcts)
        recent_profit_factor = float(stats.get("recent_profit_factor", self._recent_profit_factor(recent_net_pcts)) or 0.0)
        recent_avg_net_pct = float(
            stats.get(
                "recent_avg_net_pct",
                (sum(recent_net_pcts) / recent_trade_count) if recent_trade_count > 0 else 0.0,
            )
            or 0.0
        )
        reason = ""
        blocked = cooldown_remaining > 0
        if blocked and consecutive_losses >= int(settings["max_consecutive_losses"]):
            reason = "cooldown_consecutive_losses"
        elif blocked:
            reason = "cooldown_recent_expectancy"
        return SetupGuard(
            blocked=bool(blocked),
            cooldown_remaining=max(cooldown_remaining, 0),
            consecutive_losses=max(consecutive_losses, 0),
            recent_trade_count=max(recent_trade_count, 0),
            recent_profit_factor=round(recent_profit_factor, 6),
            recent_avg_net_pct=round(recent_avg_net_pct, 6),
            reason=reason,
        )

    def consume_setup_signal(self, *, symbol: str, timeframe: str, side: str, setup_name: str) -> None:
        settings = self._resolve_guard_settings()
        if not self.enabled or not bool(settings["enabled"]):
            return
        exact_key = self._build_keys(symbol=symbol, timeframe=timeframe, side=side, setup_name=setup_name)[0]
        stats = dict((self.state.get("stats") or {}).get(exact_key) or {})
        cooldown_remaining = int(stats.get("cooldown_remaining", 0) or 0)
        if cooldown_remaining <= 0:
            return
        stats["cooldown_remaining"] = max(cooldown_remaining - 1, 0)
        stats["updated_at_utc"] = _utc_now()
        self.state.setdefault("stats", {})[exact_key] = stats
        self.save()
