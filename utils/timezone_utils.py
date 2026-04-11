"""
Utilitários para gerenciamento de fuso horário brasileiro
"""
from datetime import datetime, timezone, timedelta
from typing import Optional

# Fuso horário do Brasil (UTC-3)
BRAZIL_TZ = timezone(timedelta(hours=-3))

def now_utc() -> datetime:
    """Retorna datetime atual em UTC com timezone info"""
    return datetime.now(timezone.utc)

def now_brazil() -> datetime:
    """Retorna datetime atual no fuso horário do Brasil (UTC-3)"""
    return datetime.now(BRAZIL_TZ)

def to_brazil(dt: datetime) -> datetime:
    """Converte datetime para fuso horário brasileiro"""
    if dt.tzinfo is None:
        # Se não tem timezone, assume UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BRAZIL_TZ)

def format_brazil_time(dt: Optional[datetime] = None, fmt: str = '%d/%m/%Y %H:%M:%S') -> str:
    """
    Formata datetime para horário brasileiro
    
    Args:
        dt: datetime a ser formatado. Se None, usa o horário atual.
        fmt: formato de saída
    
    Returns:
        String formatada no horário de Brasília
    """
    if dt is None:
        dt = now_brazil()
    elif dt.tzinfo is None:
        # Se não tem timezone, assume UTC e converte para Brasil
        dt = dt.replace(tzinfo=timezone.utc).astimezone(BRAZIL_TZ)
    else:
        # Se já tem timezone, converte para Brasil
        dt = dt.astimezone(BRAZIL_TZ)
    
    return dt.strftime(fmt)

def get_brazil_datetime_naive() -> datetime:
    """
    Retorna datetime atual do Brasil sem timezone info (para compatibilidade)
    Use com cuidado - prefira now_brazil() quando possível
    """
    return now_brazil().replace(tzinfo=None)

def parse_and_convert_to_brazil(dt_str: str, fmt: str = '%Y-%m-%d %H:%M:%S') -> datetime:
    """
    Parse string de datetime e converte para horário brasileiro
    
    Args:
        dt_str: string datetime
        fmt: formato da string de entrada
    
    Returns:
        datetime no fuso horário brasileiro
    """
    dt = datetime.strptime(dt_str, fmt)
    # Assume que a string está em UTC se não especificado
    dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BRAZIL_TZ)