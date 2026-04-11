"""Services module with lazy-safe exports."""

__all__ = []

try:
    from .binance_user_data_stream import BinanceFuturesUserDataStream
    __all__.append("BinanceFuturesUserDataStream")
except Exception:
    BinanceFuturesUserDataStream = None

try:
    from .credential_vault import CredentialVault
    __all__.append("CredentialVault")
except Exception:
    CredentialVault = None

try:
    from .multiuser_runtime_service import MultiUserRuntimeService
    __all__.append("MultiUserRuntimeService")
except Exception:
    MultiUserRuntimeService = None

try:
    from .paper_trade_service import PaperTradeService
    __all__.append("PaperTradeService")
except Exception:
    PaperTradeService = None

try:
    from .risk_management_service import RiskManagementService
    __all__.append("RiskManagementService")
except Exception:
    RiskManagementService = None
