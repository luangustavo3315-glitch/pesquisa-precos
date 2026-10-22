from __future__ import annotations

import re
from urllib.parse import quote

from .base import BaseCollector
from ..models import Product, StoreResult
from ..normalizer import strip_accents
from ..search_queries import build_search_queries


def slugify(text: str) -> str:
    text = strip_accents(text).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:120]


class PagueMenosCollector(BaseCollector):
    async def verify_store(self) -> bool:
        try:
            await self.goto(self.cfg["base_url"])
            self.store_confirmed = False
            self.store_verification_note = (
                "catálogo online acessível; o site público não oferece seleção da Loja 42 — Centro de Tietê"
            )
            return False
        except Exception as e:
            self.record_soft_failure("verify_store", e)
            self.store_verification_note = f"falha ao verificar Loja 42: {type(e).__name__}: {e}"
            return False

    async def _search_once(self, url: str):
        await self.goto(url)
        card_selector = self.selector("result_card", ".item-product")
        try:
            await self.page.locator(card_selector).first.wait_for(state="visible", timeout=15000)
        except Exception as exc:
            self.record_soft_failure("wait_result_card", exc)
        await self.page.wait_for_timeout(700)
        text = (await self.body_text()).lower()
        if "captcha" in text or "verifique que você" in text or "verifique que voce" in text:
            self.record_event("captcha_detected")
            return "captcha", []
        candidates = await self.extract_product_cards(
            card_selector=card_selector,
            title_selector=self.selector("result_title", ".title a"),
            href_selector=self.product_link_selector('.title a[href$="/p"]'),
        )
        return "ok", candidates

    async def collect(self, product: Product) -> StoreResult:
        stopped = self.circuit_result()
        if stopped is not None:
            return stopped
        queried_at = self.now()
        errors = []
        successful_searches = 0
        all_candidates = []
        seen_urls: set[str] = set()
        detail_checked: set[str] = set()
        last_detail_result = None
        try:
            cached = await self.try_cached_route(product, queried_at)
            if cached is not None:
                last_detail_result = self.choose_better_detail_result(last_detail_result, cached)
                if cached.url:
                    detail_checked.add(cached.url)
                if cached.source_match_status == "EXATO" and self._usable_detail_result(cached):
                    cached.screenshot = await self.screenshot(f"paguemenos_{product.codigo_interno or product.search_name}")
                    return cached
            queries = build_search_queries(product, store_key="paguemenos")
            max_queries = int(self.global_cfg.get("max_search_queries_per_store", 9))
            queries = queries[:max(1, max_queries)]
            for query_index, query in enumerate(queries):
                stopped = self.circuit_result()
                if stopped is not None:
                    return stopped
                self.record_event("queries_started")
                if query_index:
                    await self.sleep_polite()
                for template in self.cfg.get("search_urls", []):
                    url = template.format(query=quote(query, safe=""), query_slug=quote(slugify(query), safe="-"))
                    try:
                        state, candidates = await self._search_once(url)
                        successful_searches += 1
                        if state == "captcha":
                            self.record_hard_failure("captcha", "CAPTCHA/proteção detectada")
                            return self.circuit_result() or StoreResult(
                                self.store_key, self.cfg["name"], queried_at,
                                status="ERRO DE CONSULTA", source_match_status="ERRO DE CONSULTA",
                                store_confirmed=self.store_confirmed, url=url,
                                notes="CAPTCHA/proteção detectada; nenhuma tentativa de contorno foi feita",
                            )
                        self.record_event("query_candidates", len(candidates))
                        for candidate in candidates:
                            if candidate.url and candidate.url not in seen_urls:
                                seen_urls.add(candidate.url)
                                all_candidates.append(candidate)
                        if all_candidates:
                            listing_candidate = self.next_detail_candidate(product, all_candidates, detail_checked)
                            if listing_candidate is not None:
                                detail_checked.add(listing_candidate.url)
                                verified = await self.verify_detail_with_priority_retry(product, listing_candidate, queried_at)
                                last_detail_result = self.choose_better_detail_result(last_detail_result, verified)
                                if verified.source_match_status == "EXATO" and self._usable_detail_result(verified):
                                    self.record_event("early_detail_hits")
                                    verified.screenshot = await self.screenshot(f"paguemenos_{product.codigo_interno or product.search_name}")
                                    return verified
                                if verified.source_match_status == "DIVERGENTE":
                                    all_candidates = [c for c in all_candidates if c.url != listing_candidate.url]
                        else:
                            errors.append(f"sem candidatos em {url}")
                    except Exception as e:
                        self.record_soft_failure("search_route", e)
                        errors.append(f"{type(e).__name__}: {e}")
                        if self.circuit_open:
                            stopped = self.circuit_result()
                            if stopped is not None:
                                return stopped
                        continue
            if all_candidates or last_detail_result is not None:
                remaining = [c for c in all_candidates if c.url not in detail_checked]
                resolved = await self.resolve_best_detail(product, remaining, queried_at) if remaining else None
                result = self.choose_better_detail_result(last_detail_result, resolved)
                if result is None:
                    result = await self.pick_best(product, all_candidates, queried_at)
                if result.source_match_status == "SUBSTITUTO ACEITO" and self._usable_detail_result(result):
                    self.record_event("substitute_fallback")
                result.screenshot = await self.screenshot(f"paguemenos_{product.codigo_interno or product.search_name}")
                return result
            status = "NÃO ENCONTRADO" if successful_searches else "ERRO DE CONSULTA"
            if not successful_searches and not self.circuit_open:
                self.record_hard_failure("all_search_routes", "; ".join(errors[-3:]) or "nenhuma rota respondeu")
            if self.circuit_open:
                stopped = self.circuit_result()
                if stopped is not None:
                    return stopped
            return StoreResult(
                self.store_key, self.cfg["name"], queried_at,
                status=status, source_match_status=status,
                store_confirmed=self.store_confirmed, notes="; ".join(errors[-3:]),
            )
        except Exception as e:
            self.record_soft_failure("collect", e)
            if not self.circuit_open and "falha de navegação" not in str(e):
                self.record_hard_failure("collect", e)
            if self.circuit_open:
                stopped = self.circuit_result()
                if stopped is not None:
                    return stopped
            return StoreResult(
                self.store_key, self.cfg["name"], queried_at,
                status="ERRO DE CONSULTA", source_match_status="ERRO DE CONSULTA",
                store_confirmed=self.store_confirmed, notes=f"{type(e).__name__}: {e}",
            )
