import logging
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

import config
from config import ProductionConfig
from database.database import db as runtime_db
from position_manager import (
    build_managed_position,
    build_native_bracket_position,
    evaluate_managed_position_on_candle,
    evaluate_native_bracket_position_on_candle,
)

logger = logging.getLogger(__name__)


LONG_SIGNALS = {"COMPRA"}
SHORT_SIGNALS = {"VENDA"}
ACTIONABLE_SIGNALS = LONG_SIGNALS | SHORT_SIGNALS
RUNTIME_EXECUTION_PROFILES = {"native_bracket", "managed"}


class PaperTradeService:
    def __init__(
        self,
        database=None,
        default_stop_loss_pct: float = ProductionConfig.DEFAULT_LIVE_STOP_LOSS_PCT,
        default_take_profit_pct: float = ProductionConfig.DEFAULT_LIVE_TAKE_PROFIT_PCT,
        max_hold_candles: int = 288,
        fee_rate: Optional[float] = None,
        slippage: Optional[float] = None,
    ):
        self.database = database or runtime_db
        self.default_stop_loss_pct = float(default_stop_loss_pct)
        self.default_take_profit_pct = float(default_take_profit_pct)
        self.max_hold_candles = int(max_hold_candles)
        self.fee_rate = float(ProductionConfig.PAPER_FEE_RATE if fee_rate is None else fee_rate)
        self.slippage = float(ProductionConfig.PAPER_SLIPPAGE if slippage is None else slippage)

    @staticmethod
    def _normalize_text(value: Optional[object]) -> str:
        return str(value or "").strip().lower()

    @staticmethod
    def _normalize_comparison_timestamp(value) -> Optional[pd.Timestamp]:
        if value in (None, "", "None"):
            return None
        try:
            ts = pd.Timestamp(value)
        except Exception:
            return None
        if ts.tzinfo is not None:
            try:
                ts = ts.tz_convert("UTC").tz_localize(None)
            except Exception:
                try:
                    ts = ts.tz_localize(None)
                except Exception:
                    return None
        return ts

    @staticmethod
    def _resolve_execution_profile(execution_mode: Optional[str] = None) -> str:
        normalized_mode = str(execution_mode or "").strip().lower()
        if normalized_mode in RUNTIME_EXECUTION_PROFILES:
            return normalized_mode
        configured_mode = str(getattr(config, "EXECUTION_PROFILE", "native_bracket") or "").strip().lower()
        if configured_mode in RUNTIME_EXECUTION_PROFILES:
            return configured_mode
        return "native_bracket"

    @staticmethod
    def _resolve_runtime_close_reason(raw_reason: Optional[str]) -> str:
        normalized_reason = str(raw_reason or "").strip().lower()
        mapping = {
            "stop_loss": "STOP_LOSS",
            "take_profit": "TAKE_PROFIT",
            "stop_or_trailing": "STOP_OR_TRAILING",
            "opposite_signal": "OPPOSITE_SIGNAL",
        }
        return mapping.get(normalized_reason, str(raw_reason or "CLOSE").strip().upper() or "CLOSE")

    @staticmethod
    def _derive_trailing_active(position: Dict) -> bool:
        try:
            current_stop = float(position.get("current_stop"))
            initial_stop = float(position.get("initial_stop"))
        except (TypeError, ValueError):
            return False
        return abs(current_stop - initial_stop) > 1e-12

    def _build_trailing_trigger_price(self, side: str, entry_price: float) -> float:
        trigger_pct = float(getattr(config, "TRAILING_TRIGGER_PCT", 0.0) or 0.0) / 100.0
        if side == "long":
            return float(entry_price) * (1 + trigger_pct)
        return float(entry_price) * (1 - trigger_pct)

    def _build_managed_partial_target_price(
        self,
        side: str,
        entry_price: float,
        take_profit_pct: float,
    ) -> float:
        if side == "long":
            partial_pct = float(getattr(config, "PARTIAL_TARGET_PCT", 0.0) or 0.0) / 100.0
            return float(entry_price) * (1 + partial_pct)
        short_partial_pct = float(take_profit_pct or 0.0) * 0.55
        if short_partial_pct <= 0:
            short_partial_pct = (float(getattr(config, "SHORT_TAKE_PROFIT_PCT", 0.0) or 0.0) / 100.0) * 0.55
        return float(entry_price) * (1 - short_partial_pct)

    def _build_runtime_position_seed(
        self,
        *,
        side: str,
        entry_price: float,
        entry_timestamp,
        stop_loss_pct: float,
        take_profit_pct: float,
        atr: float = 0.0,
        execution_mode: Optional[str] = None,
        initial_stop_price: Optional[float] = None,
        initial_take_price: Optional[float] = None,
    ) -> Dict:
        execution_profile = self._resolve_execution_profile(execution_mode)
        if execution_profile == "native_bracket":
            resolved_stop_price = (
                float(initial_stop_price)
                if initial_stop_price is not None
                else float(self._build_stop_loss_price(side, entry_price, stop_loss_pct))
            )
            resolved_take_price = (
                float(initial_take_price)
                if initial_take_price is not None
                else float(self._build_take_profit_price(side, entry_price, take_profit_pct))
            )
            return build_native_bracket_position(
                side=side,
                entry_price=entry_price,
                timestamp=entry_timestamp,
                stop_price=resolved_stop_price,
                take_price=resolved_take_price,
                atr=atr,
            )

        if initial_stop_price is not None:
            resolved_stop_price = float(initial_stop_price)
        else:
            risk_distance = min(
                max(float(entry_price) * float(stop_loss_pct or 0.0), float(atr or 0.0) * 1.5),
                float(entry_price) * 0.02,
            )
            resolved_stop_price = float(entry_price) - risk_distance if side == "long" else float(entry_price) + risk_distance
        resolved_partial_target = (
            float(initial_take_price)
            if initial_take_price is not None
            else float(self._build_managed_partial_target_price(side, entry_price, take_profit_pct))
        )
        return build_managed_position(
            side=side,
            entry_price=entry_price,
            timestamp=entry_timestamp,
            stop_price=resolved_stop_price,
            partial_target_price=resolved_partial_target,
            trailing_trigger_price=self._build_trailing_trigger_price(side, entry_price),
            atr=atr,
        )

    def _snapshot_trade_from_position(self, trade: Dict, position: Dict) -> None:
        stop_loss_pct = float(trade.get("stop_loss_pct", 0.0) or 0.0)
        stop_distance_pct = stop_loss_pct if stop_loss_pct > 0 else 0.0
        trailing_active = self._derive_trailing_active(position)
        mfe_pct = round(float(position.get("mfe_pct", 0.0) or 0.0), 4)
        managed_fields = {
            "stop_loss_price": round(float(position.get("current_stop", 0.0) or 0.0), 6),
            "take_profit_price": round(float(position.get("partial_target", 0.0) or 0.0), 6),
            "final_stop_price": round(float(position.get("current_stop", 0.0) or 0.0), 6),
            "final_take_price": round(float(position.get("partial_target", 0.0) or 0.0), 6),
            "break_even_active": int(bool(position.get("break_even_active", False))),
            "trailing_active": int(bool(trailing_active)),
            "protection_level": "elevated" if bool(position.get("break_even_active", False)) or trailing_active else "normal",
            "regime_exit_flag": 0,
            "structure_exit_flag": 0,
            "post_pump_protection": 0,
            "mfe_pct": mfe_pct,
            "mae_pct": round(float(position.get("mae_pct", 0.0) or 0.0), 4),
            "max_unrealized_rr": round((mfe_pct / stop_distance_pct), 4) if stop_distance_pct > 0 else 0.0,
        }
        dirty = False
        for key, value in managed_fields.items():
            if trade.get(key) != value:
                trade[key] = value
                dirty = True
        if dirty:
            trade["_management_dirty"] = True

    def _build_runtime_close_result(
        self,
        *,
        trade: Dict,
        position_before_close: Dict,
        closed_position: Dict,
        realized_partial_pct: float = 0.0,
    ) -> Dict:
        entry_price = float(trade["entry_price"])
        persisted_fee_rate = trade.get("fee_rate")
        fee_rate = self.fee_rate if persisted_fee_rate in (None, 0, 0.0) else float(persisted_fee_rate)
        persisted_slippage = trade.get("slippage")
        slippage = self.slippage if persisted_slippage in (None, 0, 0.0) else float(persisted_slippage)
        raw_exit_price = float(closed_position["exit_price"])
        exit_price = self._apply_slippage(raw_exit_price, side=trade["side"], is_entry=False, slippage=slippage)
        if trade["side"] == "long":
            final_leg_gross_pct = ((exit_price - entry_price) / entry_price) * 100
        else:
            final_leg_gross_pct = ((entry_price - exit_price) / entry_price) * 100

        if position_before_close.get("partial_taken", False):
            gross_result_pct = (final_leg_gross_pct * 0.5) + float(realized_partial_pct or 0.0)
        else:
            gross_result_pct = final_leg_gross_pct

        entry_fee_pct = fee_rate * 100
        exit_fee_pct = ((exit_price / entry_price) * fee_rate * 100) if entry_price > 0 else fee_rate * 100
        result_pct = gross_result_pct - entry_fee_pct - exit_fee_pct
        close_reason = self._resolve_runtime_close_reason(closed_position.get("reason"))
        stop_loss_pct = float(trade.get("stop_loss_pct", 0.0) or 0.0)
        stop_distance_pct = stop_loss_pct if stop_loss_pct > 0 else 0.0
        trailing_active = self._derive_trailing_active(position_before_close)

        if result_pct > 0:
            outcome = "WIN"
        elif result_pct < 0:
            outcome = "LOSS"
        else:
            outcome = "FLAT"

        mfe_pct = round(float(closed_position.get("mfe_pct", 0.0) or 0.0), 4)
        return {
            "exit_timestamp": self._normalize_timestamp(closed_position.get("exit_timestamp")),
            "exit_price": round(float(exit_price), 6),
            "outcome": outcome,
            "close_reason": close_reason,
            "exit_reason": close_reason,
            "result_pct": round(float(result_pct), 4),
            "final_stop_price": round(float(position_before_close.get("current_stop", 0.0) or 0.0), 6),
            "final_take_price": round(float(position_before_close.get("partial_target", 0.0) or 0.0), 6),
            "break_even_active": int(bool(position_before_close.get("break_even_active", False))),
            "trailing_active": int(bool(trailing_active)),
            "protection_level": "elevated" if bool(position_before_close.get("break_even_active", False)) or trailing_active else "normal",
            "regime_exit_flag": 0,
            "structure_exit_flag": 0,
            "post_pump_protection": 0,
            "mfe_pct": mfe_pct,
            "mae_pct": round(float(closed_position.get("mae_pct", 0.0) or 0.0), 4),
            "max_unrealized_rr": round((mfe_pct / stop_distance_pct), 4) if stop_distance_pct > 0 else 0.0,
        }

    def _evaluate_trade_with_runtime_position(
        self,
        trade: Dict,
        candles: pd.DataFrame,
    ) -> Optional[Dict]:
        side = str(trade["side"]).strip().lower()
        stop_loss_pct = self._normalize_pct(trade.get("stop_loss_pct", 0.0))
        take_profit_pct = self._normalize_pct(trade.get("take_profit_pct", 0.0))
        position = self._build_runtime_position_seed(
            side=side,
            entry_price=float(trade["entry_price"]),
            entry_timestamp=trade["entry_timestamp"],
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            atr=float(trade.get("atr", 0.0) or 0.0),
            execution_mode=trade.get("execution_mode"),
            initial_stop_price=trade.get("initial_stop_price"),
            initial_take_price=trade.get("initial_take_price"),
        )
        execution_profile = self._resolve_execution_profile(trade.get("execution_mode"))
        realized_partial_pct = 0.0

        for candle_timestamp, candle in candles.iterrows():
            candle_row = candle.copy()
            candle_row["timestamp"] = candle_timestamp

            if execution_profile == "native_bracket":
                management = evaluate_native_bracket_position_on_candle(position, candle_row)
                if management["action"] == "close":
                    return self._build_runtime_close_result(
                        trade=trade,
                        position_before_close=position,
                        closed_position=management["closed_position"],
                        realized_partial_pct=0.0,
                    )
                position = management["position"]
                self._snapshot_trade_from_position(trade, position)
                continue

            management = evaluate_managed_position_on_candle(
                position,
                candle_row,
                realized_partial_pct=realized_partial_pct,
            )
            realized_partial_pct = float(management.get("realized_partial_pct", realized_partial_pct) or 0.0)
            if management["action"] == "close":
                return self._build_runtime_close_result(
                    trade=trade,
                    position_before_close=management["position_before_close"],
                    closed_position=management["closed_position"],
                    realized_partial_pct=realized_partial_pct,
                )
            position = management["position"]
            self._snapshot_trade_from_position(trade, position)

        return None

    def _recent_trade_closed_on_same_candle(
        self,
        *,
        symbol: str,
        timeframe: str,
        entry_timestamp,
        strategy_version: Optional[str] = None,
    ) -> Optional[Dict]:
        candidate_timestamp = self._normalize_comparison_timestamp(entry_timestamp)
        if candidate_timestamp is None:
            return None

        recent_trades = self.database.get_recent_paper_trades(
            limit=5,
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
        )
        for trade in recent_trades:
            if str(trade.get("status") or "").upper() != "CLOSED":
                continue
            closed_timestamp = self._normalize_comparison_timestamp(trade.get("exit_timestamp"))
            if closed_timestamp is not None and closed_timestamp == candidate_timestamp:
                return trade
        return None

    def _uses_close_to_close_execution(
        self,
        setup_name: Optional[str] = None,
        execution_mode: Optional[str] = None,
    ) -> bool:
        del setup_name, execution_mode
        return False

    @staticmethod
    def _uses_sl_tp_only_exit_mode() -> bool:
        return bool(ProductionConfig.SL_TP_ONLY_EXIT_MODE)

    def _can_flip_trade(
        self,
        current_trade: Dict,
        incoming_side: str,
        incoming_setup_name: Optional[str],
        incoming_execution_mode: Optional[str] = None,
    ) -> bool:
        del current_trade, incoming_side, incoming_setup_name, incoming_execution_mode
        return False

    def register_signal(
        self,
        symbol: str,
        timeframe: str,
        signal: str,
        entry_price: float,
        entry_timestamp,
        context_timeframe: str = None,
        source: str = "system",
        strategy_version: str = None,
        stop_loss_pct: Optional[float] = None,
        take_profit_pct: Optional[float] = None,
        risk_plan: Optional[Dict] = None,
        setup_name: str = None,
        execution_mode: str = None,
        regime: str = None,
        signal_score: Optional[float] = None,
        atr: Optional[float] = None,
        entry_reason: str = None,
        entry_quality: str = None,
        rejection_reason: str = None,
        sample_type: str = "paper",
    ) -> Optional[int]:
        signal = str(signal or "").strip().upper()
        if signal not in ACTIONABLE_SIGNALS:
            return None

        side = self._signal_to_side(signal)
        executed_entry_price = self._apply_slippage(float(entry_price), side=side, is_entry=True, slippage=self.slippage)
        stop_loss_pct = self._normalize_pct(
            self.default_stop_loss_pct if stop_loss_pct is None else stop_loss_pct
        )
        take_profit_pct = self._normalize_pct(
            self.default_take_profit_pct if take_profit_pct is None else take_profit_pct
        )
        resolved_execution_profile = self._resolve_execution_profile(execution_mode)
        timestamp_iso = self._normalize_timestamp(entry_timestamp)
        open_trades = self.database.get_open_paper_trades(symbol=symbol, timeframe=timeframe)

        if open_trades:
            primary_trade = open_trades[0]
            if len(open_trades) > 1:
                logger.warning(
                    "Mais de uma paper position aberta para %s %s; mantendo a primeira e ignorando novo sinal.",
                    symbol,
                    timeframe,
                )
            return primary_trade["id"]

        recent_same_candle_close = self._recent_trade_closed_on_same_candle(
            symbol=symbol,
            timeframe=timeframe,
            entry_timestamp=entry_timestamp,
            strategy_version=strategy_version,
        )
        if recent_same_candle_close:
            logger.info(
                "Ignorando novo signal paper para %s %s: trade #%s ja fechou no mesmo candle (%s).",
                symbol,
                timeframe,
                recent_same_candle_close.get("id"),
                recent_same_candle_close.get("exit_timestamp"),
            )
            return None

        runtime_position = self._build_runtime_position_seed(
            side=side,
            entry_price=executed_entry_price,
            entry_timestamp=timestamp_iso,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            atr=float(0.0 if pd.isna(atr) else (atr or 0.0)),
            execution_mode=resolved_execution_profile,
        )

        trade_data = {
            "symbol": symbol,
            "timeframe": timeframe,
            "context_timeframe": context_timeframe,
            "setup_name": setup_name or strategy_version,
            "execution_mode": resolved_execution_profile,
            "strategy_version": strategy_version,
            "regime": regime,
            "signal_score": 0.0 if pd.isna(signal_score) else (signal_score or 0.0),
            "atr": 0.0 if pd.isna(atr) else (atr or 0.0),
            "sample_type": sample_type,
            "signal": signal,
            "side": side,
            "source": source,
            "entry_timestamp": timestamp_iso,
            "entry_reason": entry_reason or signal,
            "entry_quality": entry_quality,
            "rejection_reason": rejection_reason,
            "entry_price": executed_entry_price,
            "stop_loss_pct": stop_loss_pct * 100,
            "take_profit_pct": take_profit_pct * 100,
            "fee_rate": self.fee_rate,
            "slippage": self.slippage,
            "stop_loss_price": runtime_position.get("current_stop"),
            "take_profit_price": runtime_position.get("partial_target"),
            "initial_stop_price": runtime_position.get("initial_stop"),
            "initial_take_price": runtime_position.get("partial_target"),
            "final_stop_price": runtime_position.get("current_stop"),
            "final_take_price": runtime_position.get("partial_target"),
            "break_even_active": bool(runtime_position.get("break_even_active", False)),
            "trailing_active": bool(self._derive_trailing_active(runtime_position)),
            "protection_level": "normal",
            "regime_exit_flag": False,
            "structure_exit_flag": False,
            "post_pump_protection": False,
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "max_unrealized_rr": 0.0,
            "planned_risk_pct": (risk_plan or {}).get("risk_per_trade_pct", 0.0),
            "planned_risk_amount": (risk_plan or {}).get("risk_amount", 0.0),
            "planned_position_notional": (risk_plan or {}).get("position_notional", 0.0),
            "planned_quantity": (risk_plan or {}).get("quantity", 0.0),
            "account_reference_balance": (risk_plan or {}).get("account_reference_balance", 0.0),
            "risk_mode": (risk_plan or {}).get("risk_mode", "normal"),
            "size_reduced": bool((risk_plan or {}).get("size_reduced", False)),
            "risk_reason": (risk_plan or {}).get("risk_reason") or (risk_plan or {}).get("reason"),
            "status": "OPEN",
            "outcome": "OPEN",
            "result_pct": 0.0,
        }
        return self.database.create_paper_trade(trade_data)

    def evaluate_open_trades(
        self,
        symbol: str,
        timeframe: str,
        market_data: pd.DataFrame,
    ) -> List[Dict]:
        if market_data is None or market_data.empty:
            return []

        market_data = market_data.sort_index()
        if "is_closed" in market_data.columns:
            closed_market_data = market_data[market_data["is_closed"].fillna(False)]
            if not closed_market_data.empty:
                market_data = closed_market_data

        open_trades = self.database.get_open_paper_trades(symbol=symbol, timeframe=timeframe)
        if not open_trades:
            return []

        closed_trades = []
        for trade in open_trades:
            entry_timestamp = pd.Timestamp(trade["entry_timestamp"])
            candles = market_data.loc[market_data.index > entry_timestamp]
            if candles.empty:
                continue

            close_result = self._evaluate_trade_with_runtime_position(trade, candles)
            if close_result is None:
                if trade.pop("_management_dirty", False):
                    self.database.update_paper_trade_management(
                        trade_id=trade["id"],
                        stop_loss_price=trade.get("stop_loss_price"),
                        take_profit_price=trade.get("take_profit_price"),
                        break_even_active=bool(trade.get("break_even_active", False)),
                        trailing_active=bool(trade.get("trailing_active", False)),
                        protection_level=trade.get("protection_level"),
                        regime_exit_flag=bool(trade.get("regime_exit_flag", False)),
                        structure_exit_flag=bool(trade.get("structure_exit_flag", False)),
                        post_pump_protection=bool(trade.get("post_pump_protection", False)),
                        mfe_pct=float(trade.get("mfe_pct", 0.0) or 0.0),
                        mae_pct=float(trade.get("mae_pct", 0.0) or 0.0),
                        max_unrealized_rr=float(trade.get("max_unrealized_rr", 0.0) or 0.0),
                    )
                continue

            self.database.close_paper_trade(
                trade_id=trade["id"],
                exit_timestamp=close_result["exit_timestamp"],
                exit_price=close_result["exit_price"],
                outcome=close_result["outcome"],
                close_reason=close_result["close_reason"],
                result_pct=close_result["result_pct"],
                final_stop_price=close_result.get("final_stop_price"),
                final_take_price=close_result.get("final_take_price"),
                break_even_active=bool(close_result.get("break_even_active", False)),
                trailing_active=bool(close_result.get("trailing_active", False)),
                protection_level=close_result.get("protection_level"),
                regime_exit_flag=bool(close_result.get("regime_exit_flag", False)),
                structure_exit_flag=bool(close_result.get("structure_exit_flag", False)),
                post_pump_protection=bool(close_result.get("post_pump_protection", False)),
                mfe_pct=float(close_result.get("mfe_pct", 0.0) or 0.0),
                mae_pct=float(close_result.get("mae_pct", 0.0) or 0.0),
                max_unrealized_rr=float(close_result.get("max_unrealized_rr", 0.0) or 0.0),
            )
            closed_trades.append({**trade, **close_result})

        return closed_trades

    def get_summary(self, symbol: str = None, timeframe: str = None) -> Dict:
        return self.database.get_paper_trade_summary(symbol=symbol, timeframe=timeframe)

    def _sync_trade_management_state(self, trade: Dict, management: Dict) -> None:
        managed_fields = {
            "stop_loss_price": management.get("stop_price"),
            "take_profit_price": management.get("take_price"),
            "final_stop_price": management.get("stop_price"),
            "final_take_price": management.get("take_price"),
            "break_even_active": int(bool(management.get("break_even_active", False))),
            "trailing_active": int(bool(management.get("trailing_active", False))),
            "protection_level": management.get("protection_level"),
            "regime_exit_flag": int(bool(management.get("regime_exit_flag", False))),
            "structure_exit_flag": int(bool(management.get("structure_exit_flag", False))),
            "post_pump_protection": int(bool(management.get("post_pump_protection", False))),
            "mfe_pct": round(float(management.get("mfe_pct", 0.0) or 0.0), 4),
            "mae_pct": round(float(management.get("mae_pct", 0.0) or 0.0), 4),
            "max_unrealized_rr": round(float(management.get("unrealized_rr", 0.0) or 0.0), 4),
        }
        dirty = False
        for key, value in managed_fields.items():
            if trade.get(key) != value:
                trade[key] = value
                dirty = True
        if dirty:
            trade["_management_dirty"] = True

    def _close_trade_on_close(self, trade: Dict, candle: pd.Series, close_reason: str) -> Dict:
        return self._build_close_result(
            trade,
            float(candle["close"]),
            self._normalize_timestamp(candle.name),
            close_reason,
        )

    @staticmethod
    def _uses_indicator_intrabar_protective_exits() -> bool:
        return bool(ProductionConfig.INDICATOR_INTRABAR_PROTECTIVE_EXITS)

    def _evaluate_close_execution_trade(
        self,
        trade: Dict,
        candles: pd.DataFrame,
    ) -> Optional[Dict]:
        return self._evaluate_trade_with_runtime_position(trade, candles)

    def _evaluate_trade_against_candles(self, trade: Dict, candles: pd.DataFrame) -> Optional[Dict]:
        return self._evaluate_trade_with_runtime_position(trade, candles)

    def _build_close_result(self, trade: Dict, exit_price: float, exit_timestamp: str, close_reason: str) -> Dict:
        entry_price = float(trade["entry_price"])
        persisted_fee_rate = trade.get("fee_rate")
        fee_rate = self.fee_rate if persisted_fee_rate in (None, 0, 0.0) else float(persisted_fee_rate)
        persisted_slippage = trade.get("slippage")
        slippage = self.slippage if persisted_slippage in (None, 0, 0.0) else float(persisted_slippage)
        exit_price = self._apply_slippage(float(exit_price), side=trade["side"], is_entry=False, slippage=slippage)
        if trade["side"] == "long":
            gross_result_pct = ((exit_price - entry_price) / entry_price) * 100
        else:
            gross_result_pct = ((entry_price - exit_price) / entry_price) * 100

        entry_fee_pct = fee_rate * 100
        exit_fee_pct = ((exit_price / entry_price) * fee_rate * 100) if entry_price > 0 else fee_rate * 100
        result_pct = gross_result_pct - entry_fee_pct - exit_fee_pct

        if result_pct > 0:
            outcome = "WIN"
        elif result_pct < 0:
            outcome = "LOSS"
        else:
            outcome = "FLAT"

        return {
            "exit_timestamp": exit_timestamp,
            "exit_price": round(float(exit_price), 6),
            "outcome": outcome,
            "close_reason": close_reason,
            "exit_reason": close_reason,
            "result_pct": round(float(result_pct), 4),
            "final_stop_price": trade.get("stop_loss_price"),
            "final_take_price": trade.get("take_profit_price"),
            "break_even_active": int(bool(trade.get("break_even_active", False))),
            "trailing_active": int(bool(trade.get("trailing_active", False))),
            "protection_level": trade.get("protection_level"),
            "regime_exit_flag": int(bool(trade.get("regime_exit_flag", False))),
            "structure_exit_flag": int(bool(trade.get("structure_exit_flag", False))),
            "post_pump_protection": int(bool(trade.get("post_pump_protection", False))),
            "mfe_pct": round(float(trade.get("mfe_pct", 0.0) or 0.0), 4),
            "mae_pct": round(float(trade.get("mae_pct", 0.0) or 0.0), 4),
            "max_unrealized_rr": round(float(trade.get("max_unrealized_rr", 0.0) or 0.0), 4),
        }

    def _apply_slippage(self, price: float, side: str, is_entry: bool, slippage: Optional[float] = None) -> float:
        effective_slippage = self.slippage if slippage is None else float(slippage)
        if effective_slippage <= 0:
            return float(price)
        if side == "long":
            return price * (1 + effective_slippage) if is_entry else price * (1 - effective_slippage)
        return price * (1 - effective_slippage) if is_entry else price * (1 + effective_slippage)

    def _signal_to_side(self, signal: str) -> str:
        return "long" if signal in LONG_SIGNALS else "short"

    def _build_stop_loss_price(self, side: str, entry_price: float, stop_loss_pct: float) -> float:
        if stop_loss_pct <= 0:
            return None
        return entry_price * (1 - stop_loss_pct if side == "long" else 1 + stop_loss_pct)

    def _build_take_profit_price(self, side: str, entry_price: float, take_profit_pct: float) -> float:
        if take_profit_pct <= 0:
            return None
        return entry_price * (1 + take_profit_pct if side == "long" else 1 - take_profit_pct)

    def _normalize_pct(self, value: float) -> float:
        raw_value = float(value or 0.0)
        return raw_value / 100 if raw_value > 1 else raw_value

    def _normalize_timestamp(self, value) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, datetime):
            return value.isoformat()
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)
