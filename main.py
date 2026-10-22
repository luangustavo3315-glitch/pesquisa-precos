from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from price_research.orchestrator import run


def parse_args():
    p = argparse.ArgumentParser(description="Pesquisa automatizada de preços — Tietê/SP")
    p.add_argument("--input", default="input/produtos_262.xlsx", help="Arquivo .xlsx ou .csv")
    p.add_argument("--config", default="config/stores.yaml")
    p.add_argument("--output-dir", default="output")
    p.add_argument("--evidence-dir", default="evidencias")
    p.add_argument("--log-dir", default="logs")
    p.add_argument("--headed", action="store_true", help="Abre o navegador visível para diagnóstico/seleção de unidade")
    p.add_argument("--limit", type=int, default=None, help="Executa somente os N primeiros produtos")
    return p.parse_args()


def abs_from_root(value: str) -> str:
    p = Path(value)
    return str(p if p.is_absolute() else ROOT / p)


if __name__ == "__main__":
    args = parse_args()
    report = asyncio.run(run(
        input_path=abs_from_root(args.input),
        config_path=abs_from_root(args.config),
        output_dir=abs_from_root(args.output_dir),
        evidence_dir=abs_from_root(args.evidence_dir),
        log_dir=abs_from_root(args.log_dir),
        headed=args.headed,
        limit=args.limit,
    ))
    print(f"\nConcluído: {report}")
