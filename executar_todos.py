"""Executa os 262 produtos pelo botão Run Python File do VS Code."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from price_research.orchestrator import run


if __name__ == "__main__":
    report = asyncio.run(
        run(
            input_path=str(ROOT / "input" / "produtos_262.xlsx"),
            config_path=str(ROOT / "config" / "stores.yaml"),
            output_dir=str(ROOT / "output"),
            evidence_dir=str(ROOT / "evidencias"),
            log_dir=str(ROOT / "logs"),
            headed=True,
            limit=None,
        )
    )
    print(f"\nConcluído: {report}")
