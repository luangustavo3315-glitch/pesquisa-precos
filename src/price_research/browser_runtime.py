from __future__ import annotations

import asyncio
import logging
import shutil
import time
from typing import Any, Callable

from .models import StoreResult

LOG = logging.getLogger("price_research")


def browser_candidates(cfg: dict) -> list[str | None]:
    configured = cfg.get("browser_executable_path")
    candidates: list[str | None] = [configured] if configured else [None]
    for name in ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "msedge"]:
        path = shutil.which(name)
        if path and path not in candidates:
            candidates.append(path)
    return candidates


async def launch_chromium(
    playwright,
    cfg: dict,
    headed: bool,
    *,
    candidates_provider: Callable[[dict], list[str | None]] = browser_candidates,
):
    errors: list[str] = []
    for executable in candidates_provider(cfg):
        try:
            kwargs: dict[str, Any] = {"headless": not headed}
            if executable:
                kwargs["executable_path"] = executable
            browser = await playwright.chromium.launch(**kwargs)
            LOG.info("Chromium iniciado%s", f" via {executable}" if executable else " via Playwright")
            return browser
        except Exception as exc:
            errors.append(f"{executable or 'playwright-default'}: {type(exc).__name__}: {exc}")
    raise RuntimeError("Não foi possível iniciar um navegador Chromium. " + " | ".join(errors[-3:]))


async def collect_one(product, collector, timeout_seconds: float):
    started = time.perf_counter()
    circuit_result = getattr(collector, "circuit_result", None)
    try:
        stopped = circuit_result() if callable(circuit_result) else None
        if stopped is not None:
            return stopped
        try:
            result = await asyncio.wait_for(
                collector.collect(product),
                timeout=max(0.01, float(timeout_seconds)),
            )
            if getattr(result, "status", "") != "ERRO DE CONSULTA":
                record_success = getattr(collector, "record_collection_success", None)
                if callable(record_success):
                    record_success()
            return result
        except TimeoutError as exc:
            record_hard_failure = getattr(collector, "record_hard_failure", None)
            if callable(record_hard_failure):
                record_hard_failure("product_timeout", exc)
            stopped = circuit_result() if callable(circuit_result) else None
            if stopped is not None:
                return stopped
            return StoreResult(
                collector.store_key,
                collector.cfg.get("name", collector.store_key),
                collector.now(),
                status="ERRO DE CONSULTA",
                source_match_status="ERRO DE CONSULTA",
                store_confirmed=collector.store_confirmed,
                notes=f"tempo limite de {float(timeout_seconds):g}s excedido; item será retomado pelo checkpoint",
            )
        except Exception as exc:
            if not getattr(collector, "circuit_open", False):
                record_hard_failure = getattr(collector, "record_hard_failure", None)
                if callable(record_hard_failure):
                    record_hard_failure("unexpected_collect", exc)
            stopped = circuit_result() if callable(circuit_result) else None
            return stopped or StoreResult(
                collector.store_key,
                collector.cfg.get("name", collector.store_key),
                collector.now(),
                status="ERRO DE CONSULTA",
                source_match_status="ERRO DE CONSULTA",
                store_confirmed=collector.store_confirmed,
                notes=f"{type(exc).__name__}: {exc}",
            )
    finally:
        record_timing = getattr(collector, "record_timing", None)
        if callable(record_timing):
            record_timing("product", time.perf_counter() - started)


async def collect_pair(product, coo, pag, parallel: bool = True, timeout_seconds: float = 180):
    """Collect both stores concurrently while products remain sequential."""
    if parallel:
        return await asyncio.gather(
            collect_one(product, coo, timeout_seconds),
            collect_one(product, pag, timeout_seconds),
        )
    return (
        await collect_one(product, coo, timeout_seconds),
        await collect_one(product, pag, timeout_seconds),
    )
