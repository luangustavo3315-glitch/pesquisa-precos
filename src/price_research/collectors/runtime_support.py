from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from datetime import datetime

from ..models import StoreResult

LOG = logging.getLogger("price_research.collector")


class RuntimeSupportMixin:
    """Browser runtime, circuit-breaker and low-level telemetry helpers."""

    def selector(self, key: str, default: str = "") -> str:
        return str((self.cfg.get("selectors") or {}).get(key) or default)

    def product_link_selector(self, default: str = 'a[href]') -> str:
        explicit = self.selector("result_link")
        if explicit:
            return explicit
        contains = str(self.cfg.get("product_href_contains") or "").strip()
        if not contains:
            return default
        # O Pague Menos usa URLs de produto terminadas em /p; a Coocerqui usa
        # /produto/ no caminho. O mesmo campo de configuração governa ambos.
        if contains == "/p":
            return 'a[href$="/p"]'
        return f'a[href*="{contains}"]'

    def record_event(self, name: str, count: int = 1) -> None:
        self.telemetry[name] += count

    def record_timing(self, name: str, seconds: float) -> None:
        if seconds >= 0:
            self.timings[name].append(float(seconds))

    @staticmethod
    def _percentile(values: list[float], q: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * q)))
        return ordered[index]

    def telemetry_report(self) -> dict:
        timing_report = {}
        for name, values in sorted(self.timings.items()):
            if not values:
                continue
            timing_report[name] = {
                "count": len(values),
                "total_seconds": round(sum(values), 3),
                "mean_seconds": round(sum(values) / len(values), 3),
                "p50_seconds": round(self._percentile(values, 0.50) or 0.0, 3),
                "p95_seconds": round(self._percentile(values, 0.95) or 0.0, 3),
                "max_seconds": round(max(values), 3),
            }
        return {"counters": self.telemetry_summary(), "timings": timing_report}

    def record_soft_failure(self, stage: str, exc: Exception | None = None) -> None:
        key = f"failure:{stage}"
        self.telemetry[key] += 1
        if exc is not None:
            LOG.debug("%s | %s | %s: %s", self.store_key, stage, type(exc).__name__, exc)

    @property
    def circuit_open(self) -> bool:
        return bool(self._circuit_open_reason)

    def record_hard_failure(self, stage: str, exc: Exception | str) -> None:
        """Abre um circuito após falhas persistentes para evitar horas parado."""
        message = str(exc)
        immediate_tokens = (
            "ERR_BLOCKED_BY_ADMINISTRATOR",
            "ERR_NAME_NOT_RESOLVED",
            "ERR_INTERNET_DISCONNECTED",
        )
        threshold = max(1, int(self.global_cfg.get("max_consecutive_hard_failures", 2)))
        immediate = any(token in message for token in immediate_tokens)
        if immediate:
            self._hard_failure_streak = threshold
        elif stage == self._hard_failure_stage:
            self._hard_failure_streak += 1
        else:
            self._hard_failure_streak = 1
        self._hard_failure_stage = stage
        self.record_event(f"hard_failure:{stage}")
        if self._hard_failure_streak >= threshold and not self._circuit_open_reason:
            self._circuit_open_reason = f"{stage}: {type(exc).__name__}: {message}"
            self.record_event("circuit_opened")
            LOG.error("Circuito %s aberto: %s", self.store_key, self._circuit_open_reason)

    def _record_navigation_success(self) -> None:
        # Uma página carregada comprova que a falha de navegação passou, mas
        # não apaga timeouts completos de produto ou CAPTCHAs consecutivos.
        if self._hard_failure_stage.startswith("navigation"):
            self._hard_failure_streak = 0
            self._hard_failure_stage = ""

    def record_collection_success(self) -> None:
        self._hard_failure_streak = 0
        self._hard_failure_stage = ""

    def circuit_result(self) -> StoreResult | None:
        if not self.circuit_open:
            return None
        return StoreResult(
            self.store_key,
            self.cfg.get("name", self.store_key),
            self.now(),
            status="ERRO DE CONSULTA",
            source_match_status="ERRO DE CONSULTA",
            store_confirmed=self.store_confirmed,
            notes=(
                "consulta interrompida pelo mecanismo antitravamento; "
                f"{self._circuit_open_reason}"
            ),
        )

    def telemetry_summary(self) -> dict[str, int]:
        return dict(sorted(self.telemetry.items()))

    def now(self) -> str:
        return datetime.now(self.tz).strftime("%Y-%m-%d %H:%M:%S")

    async def sleep_polite(self):
        lo = float(self.global_cfg.get("request_delay_min_seconds", 1.0))
        hi = float(self.global_cfg.get("request_delay_max_seconds", 2.0))
        await asyncio.sleep(random.uniform(lo, hi))

    async def goto(self, url: str):
        if self.circuit_open:
            raise RuntimeError(f"circuito antitravamento aberto: {self._circuit_open_reason}")
        timeout = int(self.global_cfg.get("navigation_timeout_ms", 30000))
        retries = max(0, int(self.global_cfg.get("navigation_retries", 2)))
        last_exc = None
        started = time.perf_counter()
        for attempt in range(retries + 1):
            self.record_event("navigation_attempts")
            try:
                response = await self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                try:
                    await self.page.wait_for_load_state("networkidle", timeout=min(timeout, 12000))
                except Exception as exc:
                    self.record_soft_failure("networkidle", exc)
                self.record_event("navigation_successes")
                self._record_navigation_success()
                self.record_timing("navigation", time.perf_counter() - started)
                return response
            except Exception as exc:
                self.record_soft_failure("navigation", exc)
                last_exc = exc
                msg = str(exc)
                # Bloqueios administrativos/DNS não melhoram com dezenas de tentativas.
                if any(token in msg for token in [
                    "ERR_BLOCKED_BY_ADMINISTRATOR",
                    "ERR_NAME_NOT_RESOLVED",
                    "ERR_INTERNET_DISCONNECTED",
                ]):
                    self.record_hard_failure("navigation", exc)
                    break
                if attempt < retries:
                    await asyncio.sleep(min(1.5 * (attempt + 1), 4.0))
        if not self.circuit_open:
            self.record_hard_failure("navigation_exhausted", last_exc or "falha desconhecida")
        self.record_timing("navigation", time.perf_counter() - started)
        raise RuntimeError(f"falha de navegação para {url}: {type(last_exc).__name__}: {last_exc}")

    async def body_text(self) -> str:
        try:
            return await self.page.locator("body").inner_text(timeout=10000)
        except Exception as exc:
            self.record_soft_failure("body_text", exc)
            return ""

    async def structured_price_text(self, root) -> str:
        """Read store-specific price nodes before falling back to broad card text."""
        fragments: list[str] = []
        for key in ("price_original", "price_current", "price_unit", "promo_badge"):
            selector = self.selector(key)
            if not selector:
                continue
            try:
                value = (await root.locator(selector).first.inner_text(timeout=900)).strip()
                if value:
                    fragments.append(value)
            except Exception as exc:
                self.record_soft_failure(f"selector:{key}", exc)
        if fragments:
            self.record_event("structured_price_hits")
        return " ".join(fragments)

    async def screenshot(self, stem: str) -> str:
        if not self.global_cfg.get("save_screenshots", True):
            return ""
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", stem)[:100]
        path = self.evidence_dir / f"{safe}.png"
        try:
            await self.page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception as exc:
            self.record_soft_failure("screenshot", exc)
            return ""
