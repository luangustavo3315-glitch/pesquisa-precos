from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .report import report_price


@dataclass(frozen=True)
class PreviousPrice:
    price: Optional[float]
    market: str
    queried_at: str


def product_history_key(product) -> str:
    """Generate the same stable key for Product objects and JSON dicts."""
    if isinstance(product, dict):
        code = product.get("codigo_interno")
        name = product.get("nome_normalizado")
        original = product.get("descricao_original")
    else:
        code = getattr(product, "codigo_interno", "")
        name = getattr(product, "nome_normalizado", "")
        original = getattr(product, "descricao_original", "")
    return str(code or name or original or "").strip()


def _reported_price(result: dict) -> Optional[float]:
    value = report_price(result)
    return float(value) if isinstance(value, (int, float)) else None


def _candidates_from_prices(prices: list[tuple[float, str, str]]) -> PreviousPrice:
    if not prices:
        return PreviousPrice(None, "", "")
    price, market, queried_at = min(prices, key=lambda item: item[0])
    return PreviousPrice(price, market, queried_at)


def _best_valid(entry: dict) -> PreviousPrice:
    candidates: list[tuple[float, str, str]] = []
    for key, label in (("coocerqui", "COOCERQUI"), ("paguemenos", "PAGUE MENOS")):
        result = entry.get(key) or {}
        price = _reported_price(result)
        if price is not None:
            candidates.append((price, label, str(result.get("queried_at") or "")))
    return _candidates_from_prices(candidates)


def _best_exact(entry: dict) -> PreviousPrice:
    return _best_valid(entry)


def current_best_price(coo_result, pag_result) -> PreviousPrice:
    candidates: list[tuple[float, str, str]] = []
    for result, label in ((coo_result, "COOCERQUI"), (pag_result, "PAGUE MENOS")):
        if result is None:
            continue
        price = report_price(result)
        if isinstance(price, (int, float)):
            candidates.append((float(price), label, str(result.queried_at or "")))
    return _candidates_from_prices(candidates)


def compute_variation(previous: PreviousPrice, current: PreviousPrice) -> dict:
    difference = None
    percent = None
    if (
        isinstance(previous.price, (int, float))
        and isinstance(current.price, (int, float))
        and previous.price > 0
    ):
        difference = round(current.price - previous.price, 2)
        percent = round((difference / previous.price) * 100, 2)
    return {
        "preco_anterior": previous.price,
        "mercado_anterior": previous.market,
        "data_anterior": previous.queried_at,
        "preco_atual": current.price,
        "mercado_atual": current.market,
        "variacao_reais": difference,
        "variacao_percentual": percent,
    }


def _records_if_historical_run(payload) -> list[dict] | None:
    """Return records only when the file is safe to use as history.

    New-format partial runs are always ignored. Legacy list-only JSON remains
    readable for backward compatibility because older releases had no run
    metadata; new releases never publish partial history in that legacy form.
    """
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    run = payload.get("run") or {}
    if run.get("status") != "complete":
        return None
    records = payload.get("records")
    return records if isinstance(records, list) else None


def load_previous_snapshot(output_dir: str | Path) -> dict[str, PreviousPrice]:
    """Load the most recent *complete* valid run, skipping partial runs."""
    output_dir = Path(output_dir)
    for path in sorted(output_dir.glob("pesquisa_precos_*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            data = _records_if_historical_run(payload)
            if data is None:
                continue
            snapshot: dict[str, PreviousPrice] = {}
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                key = product_history_key(entry.get("product") or {})
                if key:
                    snapshot[key] = _best_valid(entry)
            return snapshot
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
    return {}
