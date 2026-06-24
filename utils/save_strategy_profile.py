import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config


def main() -> None:
    parser = argparse.ArgumentParser(description="Salva um snapshot nomeado do motor ativo.")
    parser.add_argument("--name", required=True, help="Nome do perfil salvo.")
    parser.add_argument("--symbol", default=config.SYMBOL, help="Simbolo de referencia do motor.")
    parser.add_argument(
        "--context-timeframe",
        default=config.AppConfig.PRIMARY_CONTEXT_TIMEFRAME,
        help="Timeframe de contexto usado no snapshot.",
    )
    parser.add_argument("--note", default="", help="Observacao curta para lembrar o motivo do save.")
    parser.add_argument(
        "--metadata-json",
        default="",
        help="JSON opcional com metadata extra a ser mesclada ao registro salvo.",
    )
    args = parser.parse_args()

    symbol = str(args.symbol or "").strip() or config.SYMBOL
    config.apply_symbol_strategy_overrides(symbol)
    snapshot = config.build_runtime_strategy_snapshot(context_timeframe=args.context_timeframe)

    metadata = {
        "symbol_family": config.get_symbol_family_key(symbol),
        "saved_from_symbol": symbol,
        "source": "manual_cli_save",
    }
    if args.note:
        metadata["note"] = str(args.note)
    if args.metadata_json:
        extra = json.loads(args.metadata_json)
        if not isinstance(extra, dict):
            raise ValueError("metadata-json precisa ser um objeto JSON.")
        metadata.update(extra)

    result = config.save_runtime_strategy_profile(
        args.name,
        snapshot=snapshot,
        metadata=metadata,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
