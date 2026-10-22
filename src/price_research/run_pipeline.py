from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class RunState:
    run_started_perf: float
    run_id: str
    started_at: str
    cfg: dict[str, Any]
    products: list[Any]
    manifest: dict[str, Any]
    fingerprint: str
    previous_snapshot: dict[str, Any]
    route_cache: Any
    resume_file: Path
    records: list[Any]
    resumed_records: int
    telemetry: dict[str, Any]


def prepare_run(
    deps: Any,
    *,
    input_path: str,
    config_path: str,
    output_dir: str,
    log_dir: str,
    limit: int | None,
) -> RunState:
    run_started_perf = time.perf_counter()
    run_id = uuid.uuid4().hex
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cfg = deps.load_config(config_path)
    deps.setup_logging(Path(log_dir))
    products = deps.load_products(input_path)
    if limit:
        products = products[:limit]
    deps.LOG.info("Produtos carregados: %s", len(products))

    manifest = deps.build_run_manifest(
        products,
        input_path=input_path,
        config_path=config_path,
        code_version=deps.__version__,
        source_dir=Path(deps.__file__).resolve().parent,
    )
    fingerprint = deps.run_fingerprint(manifest)
    previous_snapshot = deps.load_previous_snapshot(Path(output_dir))
    route_cache = deps.ProductRouteCache(
        Path(output_dir) / deps.ROUTE_CACHE_FILENAME,
        bootstrap_output_dir=(
            Path(output_dir)
            if cfg.get("route_cache_bootstrap_previous_run", True)
            else None
        ),
    )
    resume_file = deps.checkpoint_path(output_dir)
    records = (
        deps.load_checkpoint(
            resume_file,
            products,
            expected_run_fingerprint=fingerprint,
        )
        if cfg.get("resume_checkpoint", True)
        else []
    )
    resumed_records = len(records)
    if resumed_records:
        deps.LOG.info(
            "Retomando checkpoint válido: %s/%s produtos",
            resumed_records,
            len(products),
        )
    telemetry: dict[str, Any] = {
        "records": resumed_records,
        "resumed_records": resumed_records,
        "coocerqui": {},
        "paguemenos": {},
    }
    return RunState(
        run_started_perf=run_started_perf,
        run_id=run_id,
        started_at=started_at,
        cfg=cfg,
        products=products,
        manifest=manifest,
        fingerprint=fingerprint,
        previous_snapshot=previous_snapshot,
        route_cache=route_cache,
        resume_file=resume_file,
        records=records,
        resumed_records=resumed_records,
        telemetry=telemetry,
    )


def _save_progress(deps: Any, state: RunState, coo: Any, pag: Any) -> None:
    state.telemetry = deps._telemetry_payload(
        coo,
        pag,
        len(state.records),
        state.resumed_records,
    )
    deps.save_checkpoint(
        state.resume_file,
        state.products,
        state.records,
        status="in_progress",
        telemetry=state.telemetry,
        run_fingerprint_value=state.fingerprint,
        run_manifest=state.manifest,
    )


async def _collect_loop(deps: Any, state: RunState, coo: Any, pag: Any) -> None:
    parallel = bool(state.cfg.get("parallel_stores", True))
    product_timeout = max(
        1.0,
        float(state.cfg.get("product_timeout_seconds", 180)),
    )
    checkpoint_every = max(
        1,
        int(state.cfg.get("checkpoint_every_items", 1)),
    )
    total = len(state.products)

    for idx, product in enumerate(
        state.products[state.resumed_records:],
        start=state.resumed_records + 1,
    ):
        if coo.circuit_open and pag.circuit_open:
            deps.LOG.error(
                "As duas lojas estão indisponíveis. Execução encerrada com checkpoint em %s/%s.",
                len(state.records),
                total,
            )
            break

        deps.LOG.info("[%s/%s] %s", idx, total, product.search_name)
        coo_result, pag_result = await deps._collect_pair(
            product,
            coo,
            pag,
            parallel=parallel,
            timeout_seconds=product_timeout,
        )
        if coo.circuit_open and pag.circuit_open:
            _save_progress(deps, state, coo, pag)
            deps.LOG.error(
                "As duas lojas falharam no item %s/%s. Processo interrompido e checkpoint preservado.",
                idx,
                total,
            )
            break

        deps.flag_cross_store_price_anomaly(product, coo_result, pag_result)
        state.records.append((product, coo_result, pag_result))
        deps.LOG.info(
            "  Coocerqui=%s | Pague Menos=%s",
            coo_result.status,
            pag_result.status,
        )

        if len(state.records) % checkpoint_every == 0 or len(state.records) == total:
            _save_progress(deps, state, coo, pag)


async def collect_pending(
    deps: Any,
    state: RunState,
    *,
    evidence_dir: str,
    headed: bool,
) -> None:
    if state.resumed_records >= len(state.products):
        return

    deps.save_checkpoint(
        state.resume_file,
        state.products,
        state.records,
        status="in_progress",
        telemetry=state.telemetry,
        run_fingerprint_value=state.fingerprint,
        run_manifest=state.manifest,
    )
    async with deps.async_playwright() as playwright:
        browser = await deps._launch_chromium(playwright, state.cfg, headed)
        context = await browser.new_context(
            locale="pt-BR",
            timezone_id=state.cfg.get("timezone", "America/Sao_Paulo"),
            viewport={"width": 1440, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            ),
        )
        page_coo = await context.new_page()
        page_pag = await context.new_page()
        coo = deps.CoocerquiCollector(
            page_coo,
            "coocerqui",
            state.cfg["stores"]["coocerqui"],
            state.cfg,
            Path(evidence_dir) / "coocerqui",
            route_cache=state.route_cache,
        )
        pag = deps.PagueMenosCollector(
            page_pag,
            "paguemenos",
            state.cfg["stores"]["paguemenos"],
            state.cfg,
            Path(evidence_dir) / "paguemenos",
            route_cache=state.route_cache,
        )

        if state.cfg.get("parallel_stores", True):
            await asyncio.gather(coo.verify_store(), pag.verify_store())
        else:
            await coo.verify_store()
            await pag.verify_store()
        deps.LOG.info(
            "Unidade Coocerqui confirmada: %s | %s",
            coo.store_confirmed,
            coo.store_verification_note,
        )
        deps.LOG.info(
            "Loja 42 confirmada: %s | %s",
            pag.store_confirmed,
            pag.store_verification_note,
        )

        try:
            await _collect_loop(deps, state, coo, pag)
            deps._log_telemetry("Coocerqui", coo.telemetry_summary())
            deps._log_telemetry("Pague Menos", pag.telemetry_summary())
            state.telemetry = deps._telemetry_payload(
                coo,
                pag,
                len(state.records),
                state.resumed_records,
            )
        finally:
            await browser.close()


def _serialize_records(deps: Any, state: RunState) -> list[dict[str, Any]]:
    serializable: list[dict[str, Any]] = []
    for product, coo_result, pag_result in state.records:
        entry: dict[str, Any] = {
            "product": product.__dict__,
            "coocerqui": coo_result.as_dict(),
            "paguemenos": pag_result.as_dict(),
        }
        previous = state.previous_snapshot.get(deps.product_history_key(product))
        if previous is not None:
            entry["historico"] = deps.compute_variation(
                previous,
                deps.current_best_price(coo_result, pag_result),
            )
        serializable.append(entry)
    return serializable


def finalize_run(deps: Any, state: RunState, *, output_dir: str) -> Path:
    collision_alerts = deps.suppress_duplicate_product_urls(state.records)
    state.telemetry["integrity_alerts"] = collision_alerts
    if collision_alerts:
        deps.LOG.warning(
            "Colisões de URL removidas por segurança: %s",
            len(collision_alerts),
        )

    complete = len(state.records) == len(state.products)
    checkpoint_status = "complete" if complete else "in_progress"
    report = deps.save_report(
        state.records,
        Path(output_dir),
        state.cfg.get("timezone", "America/Sao_Paulo"),
        complete=complete,
        total_products=len(state.products),
    )
    serializable = _serialize_records(deps, state)

    finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    elapsed = round(time.perf_counter() - state.run_started_perf, 3)
    run_meta = {
        "run_id": state.run_id,
        "status": "complete" if complete else "partial",
        "code_version": deps.__version__,
        "started_at": state.started_at,
        "finished_at": finished_at,
        "elapsed_seconds": elapsed,
        "runtime_total_seconds": elapsed,
        "total_products": len(state.products),
        "completed_products": len(state.records),
        "run_fingerprint": state.fingerprint,
        "run_manifest": state.manifest,
    }
    deps.write_json_atomic(
        report.with_suffix(".json"),
        {"schema_version": 2, "run": run_meta, "records": serializable},
    )

    state.telemetry["run"] = {
        "run_id": state.run_id,
        "status": run_meta["status"],
        "elapsed_seconds": elapsed,
        "runtime_total_seconds": elapsed,
        "completed_products": len(state.records),
        "total_products": len(state.products),
        "run_fingerprint": state.fingerprint,
    }
    telemetry_path = report.with_name(report.stem + "_telemetry.json")
    deps.write_json_atomic(telemetry_path, state.telemetry)
    deps.save_checkpoint(
        state.resume_file,
        state.products,
        state.records,
        status=checkpoint_status,
        telemetry=state.telemetry,
        run_fingerprint_value=state.fingerprint,
        run_manifest=state.manifest,
    )
    deps.LOG.info("Relatório salvo: %s", report)
    deps.LOG.info("Telemetria salva: %s", telemetry_path)
    if not complete:
        deps.LOG.warning(
            "Execução parcial: %s/%s produtos. Arquivo marcado como PARCIAL; rode novamente para retomar.",
            len(state.records),
            len(state.products),
        )
    return report


async def execute_run(
    deps: Any,
    *,
    input_path: str,
    config_path: str,
    output_dir: str,
    evidence_dir: str,
    log_dir: str,
    headed: bool,
    limit: int | None,
) -> Path:
    state = prepare_run(
        deps,
        input_path=input_path,
        config_path=config_path,
        output_dir=output_dir,
        log_dir=log_dir,
        limit=limit,
    )
    await collect_pending(
        deps,
        state,
        evidence_dir=evidence_dir,
        headed=headed,
    )
    return finalize_run(deps, state, output_dir=output_dir)
