import uuid
from typing import Dict, Optional

from cryptography.fernet import Fernet, InvalidToken

from config import ProductionConfig


class CredentialVault:
    """Protege credenciais de exchange com criptografia simetrica (Fernet)."""

    def __init__(self, encryption_key: Optional[str] = None, strict: bool = True):
        raw_key = (encryption_key or ProductionConfig.CREDENTIAL_ENCRYPTION_KEY or "").strip()
        self._fernet = None
        self._configured = bool(raw_key)

        if raw_key:
            self._fernet = Fernet(raw_key.encode("utf-8"))
        elif strict:
            raise ValueError(
                "CREDENTIAL_ENCRYPTION_KEY nao configurada. "
                "Configure uma chave Fernet para armazenar credenciais com seguranca."
            )

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode("utf-8")

    def is_configured(self) -> bool:
        return self._configured and self._fernet is not None

    def _require_fernet(self) -> Fernet:
        if not self._fernet:
            raise RuntimeError("CredentialVault nao configurado com chave de criptografia.")
        return self._fernet

    def encrypt(self, value: str) -> str:
        fernet = self._require_fernet()
        raw_value = (value or "").strip()
        if not raw_value:
            return ""
        return fernet.encrypt(raw_value.encode("utf-8")).decode("utf-8")

    def decrypt(self, encrypted_value: str) -> str:
        fernet = self._require_fernet()
        raw_value = (encrypted_value or "").strip()
        if not raw_value:
            return ""
        try:
            return fernet.decrypt(raw_value.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Falha ao descriptografar credencial: token invalido.") from exc

    def store_exchange_credentials(
        self,
        database,
        *,
        user_id: int,
        account_id: str,
        exchange: str,
        api_key: str,
        api_secret: str,
        credential_alias: Optional[str] = None,
        api_key_ref: Optional[str] = None,
        token_ref: Optional[str] = None,
        permissions_read: bool = True,
        permissions_trade: bool = True,
        permissions_withdraw: bool = False,
        permission_status: str = "unknown",
        token_status: str = "unknown",
        reconciliation_status: str = "unknown",
        notes: Optional[str] = None,
    ) -> int:
        encrypted_api_key = self.encrypt(api_key)
        encrypted_api_secret = self.encrypt(api_secret)
        resolved_alias = (credential_alias or f"{account_id}-{exchange}").strip()
        resolved_api_key_ref = (api_key_ref or f"ak_{uuid.uuid4().hex[:12]}").strip()
        resolved_token_ref = (token_ref or f"tk_{uuid.uuid4().hex[:12]}").strip()

        payload = {
            "user_id": int(user_id),
            "account_id": str(account_id),
            "exchange": str(exchange),
            "credential_alias": resolved_alias,
            "api_key_ref": resolved_api_key_ref,
            "token_ref": resolved_token_ref,
            "encrypted_api_key": encrypted_api_key,
            "encrypted_api_secret": encrypted_api_secret,
            "permissions_read": bool(permissions_read),
            "permissions_trade": bool(permissions_trade),
            "permissions_withdraw": bool(permissions_withdraw),
            "permission_status": permission_status or "unknown",
            "token_status": token_status or "unknown",
            "reconciliation_status": reconciliation_status or "unknown",
            "notes": notes,
        }
        return database.upsert_user_exchange_credential(payload)

    def load_exchange_credentials(
        self,
        database,
        *,
        user_id: int,
        account_id: str,
        exchange: str,
    ) -> Dict[str, str]:
        row = database.get_user_exchange_credential(
            user_id=int(user_id),
            account_id=str(account_id),
            exchange=str(exchange),
            include_encrypted=True,
        )
        if not row:
            raise ValueError("Credenciais nao encontradas para a conta informada.")

        return {
            "user_id": int(row["user_id"]),
            "account_id": str(row["account_id"]),
            "exchange": str(row["exchange"]),
            "api_key_ref": row.get("api_key_ref"),
            "token_ref": row.get("token_ref"),
            "credential_alias": row.get("credential_alias"),
            "api_key": self.decrypt(row.get("encrypted_api_key") or ""),
            "api_secret": self.decrypt(row.get("encrypted_api_secret") or ""),
        }
