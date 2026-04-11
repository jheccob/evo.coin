from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _format_value(value: Any, max_length: int = 160) -> str:
    text = repr(value)
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 3]}..."


def emit_block_debug(stage: str, **payload: Any) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if payload:
        details = ", ".join(
            f"{key}={_format_value(value)}"
            for key, value in payload.items()
        )
    else:
        details = "no_details"
    print(f"[BLOCK_DEBUG {timestamp}Z] {stage} | {details}")
