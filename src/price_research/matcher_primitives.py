from __future__ import annotations

from collections.abc import Callable
from difflib import SequenceMatcher

from .guards import semantic_text as _semantic_text, term_present as _term_present
from .matching_policy import MATCHING_POLICY
from .models import MatchDecision, Product

_token_set_ratio: Callable[[str, str], float] | None
try:
    from rapidfuzz.fuzz import token_set_ratio as _rapidfuzz_token_set_ratio
except Exception:  # pragma: no cover
    _token_set_ratio = None
else:
    _token_set_ratio = _rapidfuzz_token_set_ratio


def similarity(a: str, b: str) -> float:
    a_n, b_n = _semantic_text(a), _semantic_text(b)
    if not a_n or not b_n:
        return 0.0
    if _token_set_ratio is not None:
        return float(_token_set_ratio(a_n, b_n)) / 100.0
    return SequenceMatcher(None, a_n, b_n).ratio()


def alias_match(
    product: Product,
    evidence: str,
    candidate_title: str,
    store_key: str,
) -> tuple[bool, float, str]:
    """Recognize a reviewed alias only when its semantic tokens are present."""
    aliases = product.aliases_for_store(store_key)
    best = (False, 0.0, "")
    candidate_text = candidate_title or evidence
    candidate_n = _semantic_text(candidate_text)

    for alias in aliases:
        alias_n = _semantic_text(alias)
        if not alias_n:
            continue
        present = _term_present(alias_n, evidence)
        if not present:
            continue
        seq_sim = SequenceMatcher(None, alias_n, candidate_n).ratio() if candidate_n else 0.0
        alias_tokens = [token for token in alias_n.split() if len(token) > 2]
        hit_ratio = (
            sum(1 for token in alias_tokens if _term_present(token, evidence)) / len(alias_tokens)
            if alias_tokens
            else 0.0
        )
        score = max(seq_sim, hit_ratio)
        if score > best[1]:
            best = (True, score, alias)
    return best


def decision(
    status: str,
    confidence: float,
    reason: str,
    *,
    features: dict[str, object] | None = None,
    threshold_names: tuple[str, ...] = (),
    threshold_overrides: dict[str, float] | None = None,
    guard_failures: list[str] | None = None,
) -> MatchDecision:
    """Build a decision with auditable, dimensionally-correct calibration data."""
    thresholds = {
        name: MATCHING_POLICY.threshold_value(name)
        for name in threshold_names
    }
    if threshold_overrides:
        thresholds.update(threshold_overrides)
    return MatchDecision(
        status=status,
        confidence=confidence,
        reason=reason,
        features=dict(features or {}),
        thresholds=thresholds,
        guard_failures=list(guard_failures or []),
    )
