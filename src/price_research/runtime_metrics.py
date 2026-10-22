from __future__ import annotations

import logging
from typing import Any

LOG = logging.getLogger("price_research")


def _rate(numerator: int | float, denominator: int | float) -> float:
    if denominator <= 0:
        return 0.0
    return round(float(numerator) / float(denominator), 4)


def operational_metrics(report: dict[str, Any], records_count: int = 0) -> dict[str, float | int]:
    """Derive comparable runtime KPIs from collector counters and timings.

    Denominators prefer the number of timed product attempts because resumed
    checkpoint rows were not queried in the current browser session.
    """
    counters = dict(report.get("counters") or {})
    timings = dict(report.get("timings") or {})
    product_timing = dict(timings.get("product") or {})
    attempted_products = int(product_timing.get("count") or records_count or 0)

    queries = int(counters.get("queries_started", 0))
    detail_pages = int(counters.get("detail_pages_verified", 0))
    cache_hits = int(counters.get("route_cache_hits", 0))
    cache_misses = int(counters.get("route_cache_misses", 0))
    reloads = int(counters.get("blank_search_reloads", 0)) + int(
        counters.get("store_context_reloaded", 0)
    )
    timeouts = int(counters.get("hard_failure:product_timeout", 0))

    mean_seconds = float(product_timing.get("mean_seconds") or 0.0)
    runtime_total = float(product_timing.get("total_seconds") or round(mean_seconds * attempted_products, 3))
    return {
        "attempted_products": attempted_products,
        "runtime_total_seconds": runtime_total,
        "p50_seconds": float(product_timing.get("p50_seconds") or 0.0),
        "p95_seconds": float(product_timing.get("p95_seconds") or 0.0),
        "queries_per_product": _rate(queries, attempted_products),
        "detail_pages_per_product": _rate(detail_pages, attempted_products),
        "cache_hit_rate": _rate(cache_hits, cache_hits + cache_misses),
        "reload_rate": _rate(reloads, queries),
        "timeout_rate": _rate(timeouts, attempted_products),
    }


def collector_report(collector: Any, records_count: int) -> dict[str, Any]:
    detailed = getattr(collector, "telemetry_report", None)
    report = (
        detailed()
        if callable(detailed)
        else {"counters": collector.telemetry_summary(), "timings": {}}
    )
    report = dict(report)
    report["operational_metrics"] = operational_metrics(report, records_count)
    return report


def telemetry_payload(coo: Any, pag: Any, records_count: int, resumed_records: int = 0) -> dict[str, Any]:
    return {
        "records": records_count,
        "resumed_records": resumed_records,
        "coocerqui": collector_report(coo, records_count),
        "paguemenos": collector_report(pag, records_count),
    }


def log_telemetry(store_name: str, summary: dict[str, int]) -> None:
    failures = sum(v for k, v in summary.items() if k.startswith("failure:"))
    queries = summary.get("queries_started", 0)
    candidates = summary.get("candidates_extracted", 0)
    LOG.info(
        "Telemetria %s | queries=%s | candidatos=%s | falhas suaves=%s | detalhes=%s",
        store_name, queries, candidates, failures, summary,
    )
    if failures >= 10 and failures > max(3, candidates // 2):
        LOG.warning(
            "Telemetria %s indica possível mudança de layout/site: %s falhas suaves",
            store_name,
            failures,
        )
