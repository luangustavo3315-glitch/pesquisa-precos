from __future__ import annotations

from urllib.parse import quote

from .base import BaseCollector
from .coocerqui_support import SearchRecoveryPolicy, StoreContextManager
from ..models import Candidate, Product, StoreResult
from ..search_queries import build_search_queries


class CoocerquiCollector(BaseCollector):
    @property
    def store_context(self) -> StoreContextManager:
        manager = getattr(self, "_store_context", None)
        if manager is None:
            manager = StoreContextManager(self)
            self._store_context = manager
        return manager

    @property
    def search_recovery(self) -> SearchRecoveryPolicy:
        policy = getattr(self, "_search_recovery", None)
        if policy is None:
            policy = SearchRecoveryPolicy(self)
            self._search_recovery = policy
        return policy

    async def _selected_store_text(self) -> str:
        return await self.store_context.selected_store_text()

    def _target_store_is_selected(self, selected_text: str) -> bool:
        return self.store_context.target_store_is_selected(selected_text)

    async def _revalidate_store_context(self) -> bool:
        return await self.store_context.revalidate()

    def _reject_price_without_target_store(
        self,
        result: StoreResult,
    ) -> StoreResult:
        return self.store_context.reject_price_without_target_store(result)

    async def _load_all_product_cards(self) -> None:
        """Aciona a rolagem infinita até o catálogo parar de acrescentar cards."""
        card_selector = self.selector("result_card", ".vip-card-produto")
        cards = self.page.locator(card_selector)
        stable_rounds = 0
        max_scrolls = int(self.global_cfg.get("coocerqui_max_result_scrolls", 10))
        wait_ms = int(self.global_cfg.get("coocerqui_result_scroll_wait_ms", 1100))
        for _ in range(max(1, max_scrolls)):
            count_before = await cards.count()
            if count_before <= 0:
                return
            try:
                await cards.last.scroll_into_view_if_needed(timeout=5000)
            except Exception as exc:
                self.record_soft_failure("result_scroll", exc)
                return
            await self.page.wait_for_timeout(wait_ms)
            count_after = await cards.count()
            if count_after <= count_before:
                stable_rounds += 1
                if stable_rounds >= 2:
                    return
            else:
                stable_rounds = 0

    async def verify_store(self) -> bool:
        return await self.store_context.ensure_target_store()

    @staticmethod
    def _merge_new_candidates(all_candidates, seen_urls: set[str], candidates) -> None:
        for candidate in candidates:
            if candidate.url and candidate.url not in seen_urls:
                seen_urls.add(candidate.url)
                all_candidates.append(candidate)

    @staticmethod
    def _is_explicit_empty_search(page_text: str) -> bool:
        return SearchRecoveryPolicy.is_explicit_empty(page_text)

    async def _recover_blank_search(
        self,
        *,
        url: str,
        page_text: str,
        card_selector: str,
        title_selector: str,
        href_selector: str,
    ) -> list[Candidate]:
        return await self.search_recovery.recover(
            url=url,
            page_text=page_text,
            card_selector=card_selector,
            title_selector=title_selector,
            href_selector=href_selector,
        )

    async def _try_early_detail(
        self,
        product: Product,
        all_candidates: list[Candidate],
        detail_checked: set[str],
        queried_at: str,
        *,
        exact_only: bool = False,
    ) -> StoreResult | None:
        if not all_candidates:
            return None
        if exact_only:
            listing_candidate = next(
                (
                    candidate
                    for _confidence, status, _has_price, _available, candidate, _decision
                    in self._detail_verification_candidates(product, all_candidates)
                    if status == "EXATO" and candidate.url and candidate.url not in detail_checked
                ),
                None,
            )
            if listing_candidate is None:
                self.record_event("early_detail_skipped_non_exact")
                return None
        else:
            listing_candidate = self.next_detail_candidate(product, all_candidates, detail_checked)
        if listing_candidate is None:
            return None
        detail_checked.add(listing_candidate.url)
        return await self.verify_detail_with_priority_retry(product, listing_candidate, queried_at)

    async def verify_detail_with_priority_retry(
        self,
        product: Product,
        listing_candidate: Candidate,
        queried_at: str,
        *,
        cached_route: bool = False,
    ) -> StoreResult:
        result = await super().verify_detail_with_priority_retry(
            product,
            listing_candidate,
            queried_at,
            cached_route=cached_route,
        )
        strict = bool(self.global_cfg.get("strict_store_confirmation", True))
        allow_unconfirmed = bool(self.cfg.get("allow_unconfirmed_online", False))
        if strict and not allow_unconfirmed and self.store_confirmed:
            await self._revalidate_store_context()
        return self._reject_price_without_target_store(result)

    async def _restore_search_context(self, url: str, card_selector: str) -> None:
        """Return to the result list after an inconclusive early detail check."""
        await self.goto(url)
        try:
            await self.page.locator(card_selector).first.wait_for(state="visible", timeout=15000)
        except Exception as exc:
            self.record_soft_failure("restore_search_context", exc)
        await self.page.wait_for_timeout(300)
        self.record_event("search_context_restored")

    async def collect(self, product: Product) -> StoreResult:
        stopped = self.circuit_result()
        if stopped is not None:
            return stopped
        queried_at = self.now()
        all_candidates: list[Candidate] = []
        seen_urls: set[str] = set()
        detail_checked: set[str] = set()
        last_detail_result = None
        last_url = ""
        try:
            cached = await self.try_cached_route(product, queried_at)
            if cached is not None:
                last_detail_result = self.choose_better_detail_result(last_detail_result, cached)
                if cached.url:
                    detail_checked.add(cached.url)
                if cached.source_match_status == "EXATO" and self._usable_detail_result(cached):
                    cached.screenshot = await self.screenshot(f"coocerqui_{product.codigo_interno or product.search_name}")
                    return cached
            queries = build_search_queries(product, store_key="coocerqui")
            max_queries = int(self.global_cfg.get("max_search_queries_per_store", 9))
            queries = queries[:max(1, max_queries)]
            for query_index, query in enumerate(queries):
                self.record_event("queries_started")
                if query_index:
                    await self.sleep_polite()
                url = self.cfg["search_url"].format(query=quote(query, safe=""))
                last_url = url
                await self.goto(url)
                card_selector = self.selector("result_card", ".vip-card-produto")
                title_selector = self.selector("result_title", '[data-cy="produto-descricao"]')
                href_selector = self.product_link_selector('a[href*="/produto/"]')
                try:
                    await self.page.locator(card_selector).first.wait_for(state="visible", timeout=15000)
                except Exception as exc:
                    self.record_soft_failure("wait_result_card", exc)
                await self.page.wait_for_timeout(900)
                text = (await self.body_text()).lower()
                if "captcha" in text or "verifique que você" in text or "verifique que voce" in text:
                    self.record_event("captcha_detected")
                    self.record_hard_failure("captcha", "CAPTCHA/proteção detectada")
                    return self.circuit_result() or StoreResult(
                        self.store_key, self.cfg["name"], queried_at,
                        status="ERRO DE CONSULTA", source_match_status="ERRO DE CONSULTA",
                        store_confirmed=self.store_confirmed, url=url,
                        notes="CAPTCHA/proteção detectada; nenhuma tentativa de contorno foi feita",
                    )
                # Fast path: inspect already visible cards before triggering expensive
                # infinite-scroll expansion. Most warm/precise queries end here.
                candidates = await self.extract_product_cards(
                    card_selector=card_selector,
                    title_selector=title_selector,
                    href_selector=href_selector,
                )
                if not candidates:
                    candidates = await self._recover_blank_search(
                        url=url,
                        page_text=text,
                        card_selector=card_selector,
                        title_selector=title_selector,
                        href_selector=href_selector,
                    )
                if self.store_confirmed:
                    await self._revalidate_store_context()
                self.record_event("query_candidates", len(candidates))
                self._merge_new_candidates(all_candidates, seen_urls, candidates)
                verified = None
                if self.global_cfg.get("coocerqui_fast_path_enabled", True):
                    verified = await self._try_early_detail(
                        product,
                        all_candidates,
                        detail_checked,
                        queried_at,
                        exact_only=True,
                    )
                else:
                    self.record_event("fast_path_disabled")
                if verified is not None:
                    last_detail_result = self.choose_better_detail_result(last_detail_result, verified)
                    if verified.source_match_status == "EXATO" and self._usable_detail_result(verified):
                        self.record_event("early_detail_hits")
                        verified.screenshot = await self.screenshot(f"coocerqui_{product.codigo_interno or product.search_name}")
                        return verified
                    if verified.source_match_status == "DIVERGENTE":
                        all_candidates = [candidate for candidate in all_candidates if candidate.url != verified.url]
                    # Detail verification navigates away from the search page. A
                    # failed fast path must restore it before infinite scrolling;
                    # otherwise expansion runs against a product page and silently
                    # loses search candidates.
                    await self._restore_search_context(url, card_selector)

                # Slow path only when visible cards were insufficient.
                await self._load_all_product_cards()
                expanded = await self.extract_product_cards(
                    card_selector=card_selector,
                    title_selector=title_selector,
                    href_selector=href_selector,
                )
                self.record_event("query_candidates_expanded", len(expanded))
                self._merge_new_candidates(all_candidates, seen_urls, expanded)
                verified = await self._try_early_detail(product, all_candidates, detail_checked, queried_at)
                if verified is not None:
                    last_detail_result = self.choose_better_detail_result(last_detail_result, verified)
                    if verified.source_match_status == "EXATO" and self._usable_detail_result(verified):
                        self.record_event("early_detail_hits")
                        verified.screenshot = await self.screenshot(f"coocerqui_{product.codigo_interno or product.search_name}")
                        return verified
                    if verified.source_match_status == "DIVERGENTE":
                        all_candidates = [candidate for candidate in all_candidates if candidate.url != verified.url]

            remaining = [c for c in all_candidates if c.url not in detail_checked]
            resolved = await self.resolve_best_detail(product, remaining, queried_at) if remaining else None
            result = self.choose_better_detail_result(last_detail_result, resolved)
            if result is None:
                result = await self.pick_best(product, all_candidates, queried_at)
            if result.source_match_status == "SUBSTITUTO ACEITO" and self._usable_detail_result(result):
                self.record_event("substitute_fallback")
            if not result.url:
                result.url = last_url
            result = self._reject_price_without_target_store(result)
            result.screenshot = await self.screenshot(f"coocerqui_{product.codigo_interno or product.search_name}")
            return result
        except Exception as e:
            self.record_soft_failure("collect", e)
            if not self.circuit_open and "falha de navegação" not in str(e):
                self.record_hard_failure("collect", e)
            return StoreResult(
                self.store_key, self.cfg["name"], queried_at,
                status="ERRO DE CONSULTA", source_match_status="ERRO DE CONSULTA",
                store_confirmed=self.store_confirmed, notes=f"{type(e).__name__}: {e}",
            )
