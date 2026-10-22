from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ThresholdSpec:
    """Describe one decision boundary and the feature measured by it."""

    name: str
    feature: str
    value: float
    direction: str = "min"


@dataclass(frozen=True)
class MatchingPolicy:
    """Single source of truth for matcher decision boundaries.

    Values that merely shape confidence are kept separate from acceptance
    thresholds so calibration never compares unrelated quantities.
    """

    exact_similarity_min: float = 0.62
    exact_required_ratio_min: float = 1.0
    probable_similarity_min: float = 0.55
    probable_required_ratio_min: float = 0.65
    flexible_substitute_similarity_min: float = 0.42
    weighted_core_similarity_min: float = 0.58
    weighted_core_hit_ratio_min: float = 0.75
    weighted_core_short_hit_ratio_min: float = 1.0

    # Confidence shaping constants. These are not acceptance thresholds and are
    # intentionally absent from ``threshold_specs``.
    alias_confidence_floor: float = 0.86
    alias_confidence_cap: float = 0.98
    alias_confidence_base: float = 0.82
    alias_confidence_weight: float = 0.16
    quantity_unconfirmed_confidence: float = 0.58
    exact_confidence_floor: float = 0.80
    substitute_confidence_floor: float = 0.76

    def threshold_specs(self) -> dict[str, ThresholdSpec]:
        return {
            "exact_similarity_min": ThresholdSpec(
                "exact_similarity_min", "similarity", self.exact_similarity_min
            ),
            "exact_required_ratio_min": ThresholdSpec(
                "exact_required_ratio_min", "required_ratio", self.exact_required_ratio_min
            ),
            "probable_similarity_min": ThresholdSpec(
                "probable_similarity_min", "similarity", self.probable_similarity_min
            ),
            "probable_required_ratio_min": ThresholdSpec(
                "probable_required_ratio_min", "required_ratio", self.probable_required_ratio_min
            ),
            "flexible_substitute_similarity_min": ThresholdSpec(
                "flexible_substitute_similarity_min",
                "similarity",
                self.flexible_substitute_similarity_min,
            ),
            "weighted_core_similarity_min": ThresholdSpec(
                "weighted_core_similarity_min",
                "weighted_core_similarity",
                self.weighted_core_similarity_min,
            ),
            "weighted_core_hit_ratio_min": ThresholdSpec(
                "weighted_core_hit_ratio_min",
                "weighted_core_hit_ratio",
                self.weighted_core_hit_ratio_min,
            ),
        }

    def threshold_value(self, name: str) -> float:
        specs = self.threshold_specs()
        if name not in specs:
            raise KeyError(f"threshold desconhecido: {name}")
        return specs[name].value


MATCHING_POLICY = MatchingPolicy()
