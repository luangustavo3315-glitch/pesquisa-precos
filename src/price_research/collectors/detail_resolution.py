from __future__ import annotations

import asyncio

from ..matcher import classify
from ..models import Candidate, MatchDecision, Product, StoreResult
from ..price_evidence import normalize_weighted_candidate


class DetailResolutionMixin:
    """Rank listing candidates and verify detail pages by identity priority."""

    def _scored_candidates(self, product: Product, candidates: list[Candidate]):
        scored = []
        for candidate in candidates:
            normalize_weighted_candidate(product, candidate)
            decision = classify(product, candidate, store_key=self.store_key)
            has_price = candidate.price_observed is not None
            available = candidate.available is not False
            scored.append((decision.confidence, decision.status, has_price, available, candidate, decision))
        rank = {"EXATO": 6, "SUBSTITUTO ACEITO": 5, "PROVÁVEL": 4, "INDISPONÍVEL": 3, "DIVERGENTE": 2, "NÃO ENCONTRADO": 1}

        def priority(item):
            confidence, status, has_price, available, _candidate, _decision = item
            if status == "EXATO" and has_price and available:
                quality = 6.0
            elif status == "SUBSTITUTO ACEITO" and has_price and available:
                quality = 5.5
            elif status == "EXATO":
                quality = 5.0
            elif status == "SUBSTITUTO ACEITO":
                quality = 4.8
            else:
                quality = float(rank.get(status, 0))
            return (quality, has_price, available, confidence)

        scored.sort(key=priority, reverse=True)
        return scored

    def _detail_verification_candidates(self, product: Product, candidates: list[Candidate]):
        """Order pages to *verify* by identity before card/listing price.

        ``pick_best`` keeps the legacy commercial behavior used for listing-only
        diagnostics (for example, an unavailable exact card may lose to an
        available substitute). Detail verification has a different purpose:
        prove whether the requested product exists. Therefore every EXATO
        candidate must be checked before SUBSTITUTO ACEITO, even when the exact
        card did not expose a price.
        """
        scored = self._scored_candidates(product, candidates)
        identity_rank = {"EXATO": 3, "SUBSTITUTO ACEITO": 2, "PROVÁVEL": 1}

        def detail_priority(item):
            confidence, status, has_price, available, _candidate, _decision = item
            return (
                identity_rank.get(status, 0),
                int(available),
                int(has_price),
                float(confidence),
            )

        return sorted(scored, key=detail_priority, reverse=True)

    def next_detail_candidate(
        self,
        product: Product,
        candidates: list[Candidate],
        checked_urls: set[str],
    ) -> Candidate | None:
        for _confidence, status, _has_price, _available, candidate, _decision in self._detail_verification_candidates(product, candidates):
            if status not in {"EXATO", "SUBSTITUTO ACEITO", "PROVÁVEL"}:
                continue
            if not candidate.url or candidate.url in checked_urls:
                continue
            return candidate
        return None

    async def verify_detail_result(
        self,
        product: Product,
        listing_candidate: Candidate,
        queried_at: str,
        *,
        cached_route: bool = False,
    ) -> StoreResult:
        try:
            detail, _status_code = await self.extract_detail_candidate(listing_candidate)
            normalize_weighted_candidate(product, detail)
            # Detail-page identity is intentionally title-scoped. Long product
            # descriptions/specification tables may contain shipping weights,
            # recipe quantities or related-product names; those are audit/price
            # evidence, not SKU identity.
            identity = Candidate(
                title=detail.title,
                url=detail.url,
                raw_text=detail.title,
                price_normal=detail.price_normal,
                price_promo=detail.price_promo,
                price_observed=detail.price_observed,
                unit_price=detail.unit_price,
                available=detail.available,
                promo_condition=detail.promo_condition,
            )
            decision = classify(product, identity, store_key=self.store_key)
            result = self._finalize(product, detail, decision, queried_at)
            if decision.status in {"EXATO", "SUBSTITUTO ACEITO"}:
                if self.route_cache is not None:
                    self.route_cache.record_success(
                        product,
                        self.store_key,
                        url=detail.url,
                        title=detail.title,
                        source_match_status=decision.status,
                        confidence=decision.confidence,
                        verified_at=queried_at,
                    )
                if cached_route:
                    self.record_event("route_cache_valid_hits")
            elif cached_route and self.route_cache is not None:
                self.route_cache.invalidate(
                    product, self.store_key, reason=f"identidade mudou: {decision.reason}"
                )
                self.record_event("route_cache_invalidated")
            return result
        except Exception as exc:
            self.record_soft_failure("detail_verify", exc)
            if cached_route and self.route_cache is not None:
                message = str(exc)
                hard_invalid = any(token in message for token in ("HTTP 404", "HTTP 410", "sem título de produto confirmável"))
                failures = self.route_cache.record_failure(product, self.store_key, reason=message)
                threshold = max(1, int(self.global_cfg.get("route_cache_failures_before_invalidate", 2)))
                if hard_invalid or failures >= threshold:
                    self.route_cache.invalidate(product, self.store_key, reason=f"falhas de detalhe: {exc}")
                    self.record_event("route_cache_invalidated")
            # Never authorize a listing price when the detail page could not be verified.
            normalize_weighted_candidate(product, listing_candidate)
            listing_decision = classify(product, listing_candidate, store_key=self.store_key)
            downgraded = MatchDecision(
                "PROVÁVEL",
                min(0.69, float(listing_decision.confidence)),
                "candidato localizado na listagem, mas preço não aceito sem confirmação da página detalhada "
                f"({type(exc).__name__}: {exc})",
                features=dict(listing_decision.features),
                thresholds=dict(listing_decision.thresholds),
                guard_failures=list(listing_decision.guard_failures),
            )
            return self._finalize(product, listing_candidate, downgraded, queried_at)

    @staticmethod
    def _usable_detail_result(result: StoreResult | None) -> bool:
        """True when a verified identity has a current usable price.

        ``status`` may be PROVÁVEL only because a physical branch could not be
        confirmed (Pague Menos). Identity priority therefore uses
        ``source_match_status`` plus the captured price, not the effective
        reporting status.
        """
        return bool(
            result is not None
            and result.source_match_status in {"EXATO", "SUBSTITUTO ACEITO"}
            and result.price_observed is not None
            and result.status != "INDISPONÍVEL"
        )

    @staticmethod
    def _detail_result_price(result: StoreResult) -> float:
        values = [
            float(value)
            for value in (result.definitive_price, result.price_observed, result.price_promo, result.price_normal)
            if isinstance(value, (int, float)) and value > 0
        ]
        return min(values) if values else float("inf")

    @classmethod
    def _detail_result_priority(cls, result: StoreResult | None) -> tuple:
        """Order detail results by identity first, then commercial price.

        Exact identity with a transient price-capture failure intentionally
        outranks a priced substitute. This is conservative: a parser hiccup on
        the requested product must not silently change the product being priced.
        An explicitly unavailable exact product is different and may fall back
        to an available accepted substitute.
        """
        if result is None:
            return (0, 0, 0.0, float("-inf"))
        source = result.source_match_status
        usable = cls._usable_detail_result(result)
        unavailable = result.status == "INDISPONÍVEL" or result.availability == "INDISPONÍVEL"
        if source == "EXATO" and usable:
            quality = 60
        elif source == "EXATO" and not unavailable:
            # Exact identity exists, but the current price could not be read.
            # Do not hide this behind a different flavor/variant.
            quality = 50
        elif source == "SUBSTITUTO ACEITO" and usable:
            quality = 40
        elif source == "EXATO" and unavailable:
            quality = 35
        elif source == "SUBSTITUTO ACEITO":
            quality = 30
        elif source == "PROVÁVEL":
            quality = 20
        else:
            quality = 10
        # Lower current price wins only *inside the same identity tier*.
        price = cls._detail_result_price(result)
        price_key = -price if price != float("inf") else float("-inf")
        return (quality, int(usable), price_key, float(result.confidence or 0.0))

    @classmethod
    def choose_better_detail_result(
        cls,
        current: StoreResult | None,
        candidate: StoreResult | None,
    ) -> StoreResult | None:
        if candidate is None:
            return current
        if current is None:
            return candidate
        return candidate if cls._detail_result_priority(candidate) > cls._detail_result_priority(current) else current

    async def verify_detail_with_priority_retry(
        self,
        product: Product,
        listing_candidate: Candidate,
        queried_at: str,
        *,
        cached_route: bool = False,
    ) -> StoreResult:
        """Verify detail page and retry an EXATO whose price was not captured.

        Identity and price extraction are separate facts. If the page confirms
        the exact requested product but price parsing fails, reopen the same URL
        before any substitute can be considered.
        """
        result = await self.verify_detail_result(
            product,
            listing_candidate,
            queried_at,
            cached_route=cached_route,
        )
        if result.source_match_status == "EXATO":
            self.record_event("exact_detail_found")
            if self._usable_detail_result(result):
                self.record_event("exact_detail_accepted")
                return result
            if result.status == "INDISPONÍVEL" or result.availability == "INDISPONÍVEL":
                return result
            self.record_event("exact_detail_price_failed")
            retries = max(0, int(self.global_cfg.get("exact_detail_price_retries", 1)))
            delay_ms = max(0, int(self.global_cfg.get("exact_detail_retry_delay_ms", 250)))
            best = result
            for _ in range(retries):
                if delay_ms:
                    await asyncio.sleep(delay_ms / 1000.0)
                retry = await self.verify_detail_result(
                    product,
                    listing_candidate,
                    queried_at,
                    cached_route=cached_route,
                )
                best = self.choose_better_detail_result(best, retry) or best
                if retry.source_match_status == "EXATO":
                    self.record_event("exact_detail_found")
                    if self._usable_detail_result(retry):
                        self.record_event("exact_detail_accepted")
                        return retry
                    if retry.status != "INDISPONÍVEL" and retry.availability != "INDISPONÍVEL":
                        self.record_event("exact_detail_price_failed")
            return best
        return result

    async def try_cached_route(self, product: Product, queried_at: str) -> StoreResult | None:
        if self.route_cache is None or not self.global_cfg.get("route_cache_enabled", True):
            return None
        route = self.route_cache.get(product, self.store_key)
        if not route or not route.get("url"):
            self.record_event("route_cache_misses")
            return None
        self.record_event("route_cache_hits")
        cached = Candidate(
            title=str(route.get("title") or product.search_name),
            url=str(route["url"]),
        )
        result = await self.verify_detail_with_priority_retry(product, cached, queried_at, cached_route=True)
        if result.source_match_status in {"EXATO", "SUBSTITUTO ACEITO"}:
            return result
        return None

    async def resolve_best_detail(self, product: Product, candidates: list[Candidate], queried_at: str) -> StoreResult:
        if not candidates:
            return await self.pick_best(product, candidates, queried_at)
        scored = self._detail_verification_candidates(product, candidates)
        max_details = max(1, int(self.global_cfg.get("max_detail_candidates_per_product", 3)))
        attempted = 0
        best_verified: StoreResult | None = None
        for _confidence, status, _has_price, _available, candidate, _decision in scored:
            if status not in {"EXATO", "SUBSTITUTO ACEITO", "PROVÁVEL"}:
                continue
            result = await self.verify_detail_with_priority_retry(product, candidate, queried_at)
            attempted += 1
            best_verified = self.choose_better_detail_result(best_verified, result)
            # An accepted EXATO is unbeatable by any substitute. Stop here.
            if result.source_match_status == "EXATO" and self._usable_detail_result(result):
                return result
            if attempted >= max_details:
                break
        if best_verified is not None:
            return best_verified
        # Preserve diagnostics from the best listing candidate, but downgrade it
        # so its card price can never leak into the operational spreadsheet.
        _c, _s, _hp, _av, candidate, decision = scored[0]
        downgraded = MatchDecision(
            "PROVÁVEL" if decision.status in {"EXATO", "SUBSTITUTO ACEITO", "PROVÁVEL"} else decision.status,
            min(float(decision.confidence), 0.69)
            if decision.status in {"EXATO", "SUBSTITUTO ACEITO", "PROVÁVEL"}
            else decision.confidence,
            (
                "nenhuma página detalhada confirmou o candidato; preço da listagem não autorizado"
                if decision.status in {"EXATO", "SUBSTITUTO ACEITO", "PROVÁVEL"}
                else decision.reason
            ),
            features=dict(decision.features),
            thresholds=dict(decision.thresholds),
            guard_failures=list(decision.guard_failures),
        )
        return self._finalize(product, candidate, downgraded, queried_at)
