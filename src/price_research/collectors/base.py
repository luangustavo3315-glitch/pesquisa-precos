from __future__ import annotations

from abc import ABC, abstractmethod
from collections import Counter, defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

from ..models import Candidate, MatchDecision, Product, StoreResult
from ..price_evidence import (  # noqa: F401
    detect_sale_unit,
    extract_average_weight_kg,
    extract_direct_per_kg_prices,
    extract_labeled_per_kg_price,
    extract_sale_weight_basis_kg,
    money_to_float,
    normalize_weighted_candidate,
    parse_prices,
)
from .candidate_extraction import CandidateExtractionMixin
from .detail_extraction import DetailExtractionMixin
from .detail_resolution import DetailResolutionMixin
from .runtime_support import RuntimeSupportMixin


class BaseCollector(
    RuntimeSupportMixin,
    CandidateExtractionMixin,
    DetailExtractionMixin,
    DetailResolutionMixin,
    ABC,
):
    """Shared collector facade.

    Browser runtime, candidate extraction and detail verification live in focused
    mixins. This class owns only shared state plus final result policy.
    """
    def __init__(
        self,
        page,
        store_key: str,
        store_cfg: dict,
        global_cfg: dict,
        evidence_dir: Path,
        route_cache=None,
    ):
        self.page = page
        self.store_key = store_key
        self.cfg = store_cfg
        self.global_cfg = global_cfg
        self.evidence_dir = evidence_dir
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.route_cache = route_cache
        self.store_confirmed = False
        self.store_verification_note = ""
        self.tz = ZoneInfo(global_cfg.get("timezone", "America/Sao_Paulo"))
        self.telemetry: Counter[str] = Counter()
        self.timings: dict[str, list[float]] = defaultdict(list)
        self._hard_failure_streak = 0
        self._hard_failure_stage = ""
        self._circuit_open_reason = ""

    def _finalize(self, product: Product, candidate: Candidate, decision: MatchDecision, queried_at: str) -> StoreResult:
        strict = bool(self.global_cfg.get("strict_store_confirmation", True))
        allow_unconfirmed = bool(self.cfg.get("allow_unconfirmed_online", False))
        effective_status = decision.status
        notes = decision.reason
        if candidate.price_normalization_note:
            notes += f"; {candidate.price_normalization_note}"

        if candidate.available is False and decision.status in {"EXATO", "SUBSTITUTO ACEITO", "PROVÁVEL"}:
            effective_status = "INDISPONÍVEL"
        elif decision.status in {"EXATO", "SUBSTITUTO ACEITO"} and candidate.price_observed is None:
            effective_status = "PROVÁVEL"
            notes += "; produto compatível, mas nenhum preço foi capturado nesse cartão"
        elif decision.status in {"EXATO", "SUBSTITUTO ACEITO"} and strict and not (self.store_confirmed or allow_unconfirmed):
            effective_status = "PROVÁVEL"
            notes += "; correspondência do produto confirmada, mas a unidade solicitada não foi confirmada"

        definitive = None
        if effective_status in {"EXATO", "SUBSTITUTO ACEITO"} and candidate.available is not False:
            displayed = [
                float(value) for value in (candidate.price_promo, candidate.price_observed, candidate.price_normal)
                if isinstance(value, (int, float)) and value > 0
            ]
            definitive = min(displayed) if displayed else None

        source_scope = (
            "branch_confirmed" if self.store_confirmed
            else "online_unconfirmed_accepted" if allow_unconfirmed
            else "online_unconfirmed_branch"
        )

        return StoreResult(
            store_key=self.store_key,
            store_name=self.cfg.get("name", self.store_key),
            queried_at=queried_at,
            status=effective_status,
            source_match_status=decision.status,
            confidence=decision.confidence,
            definitive_price=definitive,
            price_normal=candidate.price_normal,
            price_promo=candidate.price_promo,
            price_observed=candidate.price_observed,
            unit_price=candidate.unit_price or product.unidade_preco,
            store_confirmed=self.store_confirmed,
            found_name=candidate.title,
            url=candidate.url,
            promo_condition=candidate.promo_condition,
            availability="INDISPONÍVEL" if candidate.available is False else "DISPONÍVEL" if candidate.available else "NÃO CONFIRMADA",
            raw_text=candidate.raw_text[:10000],
            notes=(notes + (f"; {self.store_verification_note}" if self.store_verification_note else "")).strip("; "),
            price_basis=candidate.price_basis,
            price_validation=candidate.price_validation,
            raw_price_label=candidate.raw_price_label,
            price_evidence_schema=candidate.price_evidence_schema,
            price_evidence_value=candidate.price_evidence_value,
            price_evidence_source_url=candidate.price_evidence_source_url or candidate.url,
            price_evidence_captured_at=queried_at,
            source_scope=source_scope,
            match_features=dict(getattr(decision, "features", {}) or {}),
            match_thresholds=dict(getattr(decision, "thresholds", {}) or {}),
            match_guard_failures=list(getattr(decision, "guard_failures", []) or []),
        )

    async def pick_best(self, product: Product, candidates: list[Candidate], queried_at: str) -> StoreResult:
        if not candidates:
            return StoreResult(
                store_key=self.store_key, store_name=self.cfg.get("name", self.store_key),
                queried_at=queried_at, status="NÃO ENCONTRADO",
                source_match_status="NÃO ENCONTRADO",
                store_confirmed=self.store_confirmed, notes=self.store_verification_note,
            )
        scored = self._scored_candidates(product, candidates)
        _, _, _, _, cand, dec = scored[0]
        return self._finalize(product, cand, dec, queried_at)

    @abstractmethod
    async def verify_store(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def collect(self, product: Product) -> StoreResult:
        raise NotImplementedError
