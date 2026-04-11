from __future__ import annotations

import runpy
from pathlib import Path


def main() -> None:
    runner_path = Path(__file__).resolve().with_name("bot_runner.py")
    runpy.run_path(str(runner_path), run_name="__main__")


if __name__ == "__main__":
    main()
