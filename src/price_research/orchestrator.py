from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import yaml
from playwright.async_api import async_playwright

from . import __version__
from .browser_runtime import (
    browser_candidates as _browser_candidates_impl,
    collect_one as _collect_one_impl,
    launch_chromium as _launch_chromium_impl,
)
from .checkpoint import (
    build_run_manifest,
    checkpoint_path,
    load_checkpoint,
    run_fingerprint,
    save_checkpoint,
    write_json_atomic,
)
from .collectors.coocerqui import CoocerquiCollector
from .collectors.paguemenos import PagueMenosCollector
from .history import (
    compute_variation,
    current_best_price,
    load_previous_snapshot,
    product_history_key,
)
from .input_loader import load_products
from .integrity import suppress_duplicate_product_urls
from .report import report_price, save_report
from .route_cache import ProductRouteCache, ROUTE_CACHE_FILENAME
from .run_pipeline import execute_run
from .runtime_metrics import (
    log_telemetry as _log_telemetry_impl,
    telemetry_payload as _telemetry_payload_impl,
)

LOG = logging.getLogger("price_research")


def flag_cross_store_price_anomaly(
    product,
    coo_result,
    pag_result,
    ratio_threshold: float = 2.0,
) -> bool:
    """Mark extreme cross-store differences without deleting valid evidence."""
    coo_price = report_price(coo_result)
    pag_price = report_price(pag_result)
    if not isinstance(coo_price, (int, float)) or not isinstance(pag_price, (int, float)):
        return False
    if coo_price <= 0 or pag_price <= 0:
        return False
    ratio = max(float(coo_price), float(pag_price)) / min(
        float(coo_price),
        float(pag_price),
    )
    if ratio < ratio_threshold:
        return False
    note = (
        f"ALERTA DE PREÇO: diferença entre mercados de {ratio:.2f}x "
        f"(Coocerqui R$ {coo_price:.2f}; Pague Menos R$ {pag_price:.2f}); "
        "revisar unidade/promoção"
    )
    coo_result.notes = (coo_result.notes + "; " + note).strip("; ")
    pag_result.notes = (pag_result.notes + "; " + note).strip("; ")
    LOG.warning("%s | %s", product.search_name, note)
    return True


def _browser_candidates(cfg: dict) -> list[str | None]:
    return _browser_candidates_impl(cfg)


async def _launch_chromium(playwright, cfg: dict, headed: bool):
    return await _launch_chromium_impl(
        playwright,
        cfg,
        headed,
        candidates_provider=_browser_candidates,
    )


def load_config(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(
                log_dir / "pesquisa_precos.log",
                encoding="utf-8",
            ),
            logging.StreamHandler(),
        ],
    )


async def _collect_one(product, collector, timeout_seconds: float):
    return await _collect_one_impl(product, collector, timeout_seconds)


async def _collect_pair(
    product,
    coo,
    pag,
    parallel: bool = True,
    timeout_seconds: float = 180,
):
    if parallel:
        return await asyncio.gather(
            _collect_one(product, coo, timeout_seconds),
            _collect_one(product, pag, timeout_seconds),
        )
    return (
        await _collect_one(product, coo, timeout_seconds),
        await _collect_one(product, pag, timeout_seconds),
    )


def _telemetry_payload(
    coo,
    pag,
    records_count: int,
    resumed_records: int = 0,
) -> dict:
    return _telemetry_payload_impl(
        coo,
        pag,
        records_count,
        resumed_records,
    )


def _log_telemetry(store_name: str, summary: dict[str, int]) -> None:
    _log_telemetry_impl(store_name, summary)


async def run(
    input_path: str,
    config_path: str,
    output_dir: str,
    evidence_dir: str,
    log_dir: str,
    headed: bool = False,
    limit: int | None = None,
) -> Path:
    """Run the pipeline through a dependency-injected orchestration engine.

    Passing this module as the dependency surface preserves test monkeypatching
    while keeping the public orchestrator small and stable.
    """
    return await execute_run(
        sys.modules[__name__],
        input_path=input_path,
        config_path=config_path,
        output_dir=output_dir,
        evidence_dir=evidence_dir,
        log_dir=log_dir,
        headed=headed,
        limit=limit,
    )
