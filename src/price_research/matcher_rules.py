from __future__ import annotations

from dataclasses import dataclass, field

from .guards import (
    brand_present as _brand_present,
    core_weighted_name as _core_weighted_name,
    detect_product_type,
    fresh_modifier_guard as _fresh_modifier_guard,
    required_term_present as _required_term_present,
    run_preclassification_guards,
    same_explicit_quantity as _same_explicit_quantity,
    same_flexible_variant_family as _same_flexible_variant_family,
    same_product_type as _same_product_type,
    term_present as _term_present,
    weighted_generic_extra_guard as _weighted_generic_extra_guard,
)
from .matcher_primitives import alias_match, decision, similarity
from .matching_policy import MATCHING_POLICY
from .models import Candidate, MatchDecision, Product
from .normalizer import comparable_quantity_match, normalize_text


@dataclass
class MatchContext:
    product: Product
    candidate: Candidate
    store_key: str
    target: str = field(init=False)
    evidence: str = field(init=False)
    features: dict[str, object] = field(init=False)
    brand_ok: bool = True

    def __post_init__(self) -> None:
        self.target = self.product.search_name
        self.evidence = " ".join(
            value for value in [self.candidate.title, self.candidate.raw_text] if value
        )
        self.features = {
            "store_key": self.store_key,
            "category": self.product.categoria,
            "brand": self.product.marca,
            "target_unit": self.product.unidade_preco,
            "pricing_mode": "per_kg" if self.product.unidade_preco == "kg" else "unit",
        }


def _preclassification_decision(ctx: MatchContext) -> MatchDecision | None:
    if not normalize_text(ctx.evidence):
        return decision(
            "DIVERGENTE",
            0.0,
            "resultado sem texto suficiente para validação",
            features=ctx.features,
            guard_failures=["empty_evidence"],
        )

    guard_failure = run_preclassification_guards(
        ctx.product, ctx.candidate, ctx.evidence, ctx.store_key
    )
    if guard_failure:
        return decision(
            "DIVERGENTE",
            guard_failure.confidence,
            guard_failure.reason,
            features=ctx.features,
            guard_failures=[guard_failure.name],
        )

    for excluded in ctx.product.termos_excluidos:
        if excluded and _term_present(excluded, ctx.evidence):
            return decision(
                "DIVERGENTE",
                0.15,
                f"termo excluído encontrado: {excluded}",
                features=ctx.features,
                guard_failures=["excluded_term"],
            )

    guarded_type = detect_product_type(ctx.target)
    if guarded_type:
        type_ok, _ = _same_product_type(ctx.target, ctx.evidence)
        ctx.features["product_type_match"] = type_ok
        if not type_ok:
            return decision(
                "DIVERGENTE",
                0.28,
                f"tipo de produto incompatível: esperado {guarded_type}",
                features=ctx.features,
                guard_failures=["product_type"],
            )

    if ctx.product.marca:
        ctx.brand_ok = _brand_present(ctx.product.marca, ctx.evidence)
        ctx.features["brand_match"] = ctx.brand_ok
        if not ctx.brand_ok:
            return decision(
                "DIVERGENTE",
                0.30,
                f"marca esperada não confirmada: {ctx.product.marca}",
                features=ctx.features,
                guard_failures=["brand"],
            )
    else:
        ctx.features["brand_match"] = True

    q_ok, q_reason = comparable_quantity_match(ctx.target, ctx.evidence)
    ctx.features["quantity_match"] = q_ok
    if q_ok:
        return None
    if q_reason.startswith("quantidade não confirmada"):
        return decision(
            "PROVÁVEL",
            MATCHING_POLICY.quantity_unconfirmed_confidence,
            q_reason,
            features=ctx.features,
            guard_failures=["quantity_unconfirmed"],
        )
    return decision(
        "DIVERGENTE",
        0.25,
        q_reason,
        features=ctx.features,
        guard_failures=["quantity"],
    )


def _alias_decision(ctx: MatchContext) -> MatchDecision | None:
    alias_ok, alias_sim, alias_used = alias_match(
        ctx.product, ctx.evidence, ctx.candidate.title, ctx.store_key
    )
    ctx.features["alias_score"] = round(alias_sim, 6)
    ctx.features["alias_match"] = alias_ok
    if not (alias_ok and ctx.brand_ok):
        return None
    confidence = round(
        max(
            MATCHING_POLICY.alias_confidence_floor,
            min(
                MATCHING_POLICY.alias_confidence_cap,
                MATCHING_POLICY.alias_confidence_base
                + MATCHING_POLICY.alias_confidence_weight * alias_sim,
            ),
        ),
        3,
    )
    return decision(
        "EXATO",
        confidence,
        f"mesmo produto reconhecido por nomenclatura alternativa: {alias_used}",
        features=ctx.features,
    )


def _weighted_decision(ctx: MatchContext) -> MatchDecision | None:
    if ctx.product.unidade_preco != "kg":
        return None

    weighted_extra_ok, weighted_extra_reason = _weighted_generic_extra_guard(
        ctx.target, ctx.candidate.title or ctx.evidence
    )
    if not weighted_extra_ok:
        return decision(
            "DIVERGENTE",
            0.21,
            weighted_extra_reason,
            features=ctx.features,
            guard_failures=["weighted_extra"],
        )

    core_target = _core_weighted_name(ctx.target)
    core_evidence = _core_weighted_name(ctx.candidate.title or ctx.evidence)
    core_sim = similarity(core_target, core_evidence)
    core_tokens = [token for token in normalize_text(core_target).split() if len(token) > 2]
    hit_ratio = (
        sum(1 for token in core_tokens if _term_present(token, ctx.evidence)) / len(core_tokens)
        if core_tokens
        else 0.0
    )
    modifier_ok, modifier_reason = _fresh_modifier_guard(core_target, core_evidence)
    ctx.features["weighted_core_similarity"] = round(core_sim, 6)
    ctx.features["weighted_core_hit_ratio"] = round(hit_ratio, 6)
    if not modifier_ok:
        return decision(
            "DIVERGENTE",
            0.24,
            modifier_reason,
            features=ctx.features,
            guard_failures=["fresh_modifier"],
        )

    min_hit = (
        MATCHING_POLICY.weighted_core_short_hit_ratio_min
        if len(core_tokens) <= 2
        else MATCHING_POLICY.weighted_core_hit_ratio_min
    )
    if not (
        ctx.brand_ok
        and hit_ratio >= min_hit
        and core_sim >= MATCHING_POLICY.weighted_core_similarity_min
    ):
        return None

    confidence = round(
        max(0.82, min(0.98, 0.58 * core_sim + 0.42 * hit_ratio)),
        3,
    )
    return decision(
        "EXATO",
        confidence,
        "produto por peso compatível; nome/corte e unidade kg preservados",
        features=ctx.features,
        threshold_names=("weighted_core_similarity_min",),
        threshold_overrides={"weighted_core_hit_ratio_min": min_hit},
    )


def _similarity_features(ctx: MatchContext) -> tuple[list[str], float, float, float]:
    required = [term for term in ctx.product.termos_obrigatorios if term]
    hits = sum(1 for term in required if _required_term_present(term, ctx.evidence))
    req_ratio = hits / len(required) if required else 1.0
    sim = similarity(ctx.target, ctx.candidate.title or ctx.evidence)
    confidence = round((0.58 * sim) + (0.42 * req_ratio), 3)
    ctx.features["required_ratio"] = round(req_ratio, 6)
    ctx.features["similarity"] = round(sim, 6)
    return required, req_ratio, sim, confidence


def _exact_decision(
    ctx: MatchContext, req_ratio: float, sim: float, confidence: float
) -> MatchDecision | None:
    if not (
        req_ratio >= MATCHING_POLICY.exact_required_ratio_min
        and sim >= MATCHING_POLICY.exact_similarity_min
        and ctx.brand_ok
    ):
        return None
    return decision(
        "EXATO",
        max(confidence, MATCHING_POLICY.exact_confidence_floor),
        "marca, variante/termos e quantidade compatíveis",
        features=ctx.features,
        threshold_names=("exact_similarity_min", "exact_required_ratio_min"),
    )


def _explicit_substitute_decision(
    ctx: MatchContext, required: list[str], sim: float
) -> MatchDecision | None:
    substitutable = [term for term in ctx.product.termos_substituiveis if term]
    if not substitutable:
        return None
    invariant = [term for term in required if term not in substitutable]
    invariant_ok = all(_required_term_present(term, ctx.evidence) for term in invariant)
    type_ok, product_type = _same_product_type(ctx.target, ctx.evidence)
    quantity_exact = _same_explicit_quantity(ctx.target, ctx.evidence)
    missing_variant = [term for term in substitutable if not _term_present(term, ctx.evidence)]
    ctx.features["substitute_invariants_match"] = invariant_ok
    ctx.features["substitute_type_match"] = type_ok
    ctx.features["substitute_quantity_exact"] = quantity_exact
    if not (invariant_ok and type_ok and quantity_exact and missing_variant):
        return None
    substitute_confidence = round(
        max(
            MATCHING_POLICY.substitute_confidence_floor,
            min(0.94, (0.55 * sim) + 0.39),
        ),
        3,
    )
    return decision(
        "SUBSTITUTO ACEITO",
        substitute_confidence,
        "variante alternativa aceita; tipo, marca e quantidade preservados "
        f"({product_type}); variante pedida não confirmada: {', '.join(missing_variant)}",
        features=ctx.features,
    )


def _flexible_substitute_decision(ctx: MatchContext, sim: float) -> MatchDecision | None:
    family_ok, family = _same_flexible_variant_family(
        ctx.target, ctx.candidate.title or ctx.evidence
    )
    flexible_quantity_exact = _same_explicit_quantity(ctx.target, ctx.evidence)
    ctx.features["flexible_family_match"] = family_ok
    if not (
        family_ok
        and ctx.brand_ok
        and flexible_quantity_exact
        and sim >= MATCHING_POLICY.flexible_substitute_similarity_min
    ):
        return None
    return decision(
        "SUBSTITUTO ACEITO",
        round(max(0.76, min(0.92, 0.58 + 0.34 * sim)), 3),
        f"variante/sabor alternativo aceito; marca, família {family} e quantidade preservados",
        features=ctx.features,
        threshold_names=("flexible_substitute_similarity_min",),
    )


def _probable_or_divergent_decision(
    ctx: MatchContext,
    required: list[str],
    req_ratio: float,
    sim: float,
    confidence: float,
) -> MatchDecision:
    threshold_names = ("probable_required_ratio_min", "probable_similarity_min")
    if (
        req_ratio >= MATCHING_POLICY.probable_required_ratio_min
        and sim >= MATCHING_POLICY.probable_similarity_min
        and ctx.brand_ok
    ):
        missing = [term for term in required if not _term_present(term, ctx.evidence)]
        return decision(
            "PROVÁVEL",
            confidence,
            "dados não confirmados na página/listagem: " + ", ".join(missing[:5]),
            features=ctx.features,
            threshold_names=threshold_names,
        )
    return decision(
        "DIVERGENTE",
        confidence,
        f"semelhança insuficiente (similaridade={sim:.2f}, termos={req_ratio:.2f})",
        features=ctx.features,
        threshold_names=threshold_names,
    )


def classify(product: Product, candidate: Candidate, store_key: str = "") -> MatchDecision:
    """Classify one candidate through explicit, auditable decision stages."""
    ctx = MatchContext(product, candidate, store_key)

    preclassified = _preclassification_decision(ctx)
    if preclassified is not None:
        return preclassified

    alias_result = _alias_decision(ctx)
    if alias_result is not None:
        return alias_result

    weighted_result = _weighted_decision(ctx)
    if weighted_result is not None:
        return weighted_result

    required, req_ratio, sim, confidence = _similarity_features(ctx)

    exact_result = _exact_decision(ctx, req_ratio, sim, confidence)
    if exact_result is not None:
        return exact_result

    explicit_substitute = _explicit_substitute_decision(ctx, required, sim)
    if explicit_substitute is not None:
        return explicit_substitute

    flexible_substitute = _flexible_substitute_decision(ctx, sim)
    if flexible_substitute is not None:
        return flexible_substitute

    return _probable_or_divergent_decision(
        ctx, required, req_ratio, sim, confidence
    )
