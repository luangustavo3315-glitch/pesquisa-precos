from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field, is_dataclass
from math import log, log2
from statistics import mean
from typing import Iterable, Mapping, Sequence

from .matching_policy import MATCHING_POLICY


@dataclass(frozen=True)
class FragileDecision:
    index: int
    status: str
    threshold_name: str
    feature: str
    feature_value: float
    threshold_value: float
    distance: float
    direction: str


@dataclass
class RunSummary:
    total: int
    status_counts: dict[str, int]
    status_rates: dict[str, float]
    mean_confidence: float | None
    feature_stats: dict[str, dict[str, float | int]]
    segments: dict[str, "RunSummary"] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        return data


def _record_to_mapping(record: object) -> Mapping[str, object]:
    if isinstance(record, Mapping):
        return record
    if is_dataclass(record):
        return asdict(record)
    if hasattr(record, "as_dict"):
        value = record.as_dict()
        if isinstance(value, Mapping):
            return value
    raise TypeError(f"registro de calibração não suportado: {type(record).__name__}")


def _status(record: Mapping[str, object]) -> str:
    return str(
        record.get("source_match_status")
        or record.get("status")
        or ""
    )


def _confidence(record: Mapping[str, object]) -> float | None:
    value = record.get("confidence")
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _features(record: Mapping[str, object]) -> Mapping[str, object]:
    value = record.get("match_features")
    if not isinstance(value, Mapping):
        value = record.get("features")
    return value if isinstance(value, Mapping) else {}


def _thresholds(record: Mapping[str, object]) -> Mapping[str, object]:
    value = record.get("match_thresholds")
    if not isinstance(value, Mapping):
        value = record.get("thresholds")
    return value if isinstance(value, Mapping) else {}


def _numeric_feature_values(
    records: Sequence[Mapping[str, object]],
) -> dict[str, list[float]]:
    values: dict[str, list[float]] = {}
    for record in records:
        for name, value in _features(record).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            values.setdefault(str(name), []).append(float(value))
    return values


def _quantile(values: Sequence[float], q: float) -> float:
    if not values:
        raise ValueError("quantile requer valores")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lo = int(position)
    hi = min(lo + 1, len(ordered) - 1)
    fraction = position - lo
    return ordered[lo] * (1.0 - fraction) + ordered[hi] * fraction


def _feature_stats(
    values: Mapping[str, Sequence[float]],
) -> dict[str, dict[str, float | int]]:
    output: dict[str, dict[str, float | int]] = {}
    for name, series in values.items():
        if not series:
            continue
        output[name] = {
            "count": len(series),
            "mean": round(mean(series), 6),
            "min": round(min(series), 6),
            "q10": round(_quantile(series, 0.10), 6),
            "q50": round(_quantile(series, 0.50), 6),
            "q90": round(_quantile(series, 0.90), 6),
            "max": round(max(series), 6),
        }
    return output


def _segment_value(record: Mapping[str, object], field_name: str) -> str:
    value = record.get(field_name)
    if value in (None, ""):
        value = _features(record).get(field_name)
    if value in (None, ""):
        return "<missing>"
    return str(value)


def summarize_run(
    records: Iterable[object],
    *,
    segment_by: Sequence[str] = (),
) -> RunSummary:
    mapped = [_record_to_mapping(record) for record in records]
    counts = Counter(_status(record) or "<unknown>" for record in mapped)
    total = len(mapped)
    confidences = [
        value
        for record in mapped
        if (value := _confidence(record)) is not None
    ]
    summary = RunSummary(
        total=total,
        status_counts=dict(sorted(counts.items())),
        status_rates={
            key: round(count / total, 6) if total else 0.0
            for key, count in sorted(counts.items())
        },
        mean_confidence=round(mean(confidences), 6) if confidences else None,
        feature_stats=_feature_stats(_numeric_feature_values(mapped)),
    )
    if segment_by:
        grouped: dict[str, list[Mapping[str, object]]] = {}
        for record in mapped:
            key = " | ".join(
                f"{field_name}={_segment_value(record, field_name)}"
                for field_name in segment_by
            )
            grouped.setdefault(key, []).append(record)
        summary.segments = {
            key: summarize_run(group)
            for key, group in sorted(grouped.items())
        }
    return summary


def flag_fragile_decisions(
    records: Iterable[object],
    *,
    margin: float = 0.03,
) -> list[FragileDecision]:
    """Flag only feature/threshold pairs that the matcher actually evaluated.

    This intentionally never compares ``confidence`` with similarity, ratio or
    guard thresholds.
    """
    specs = MATCHING_POLICY.threshold_specs()
    fragile: list[FragileDecision] = []
    for index, raw_record in enumerate(records):
        record = _record_to_mapping(raw_record)
        features = _features(record)
        for threshold_name, raw_threshold in _thresholds(record).items():
            spec = specs.get(str(threshold_name))
            if spec is None:
                continue
            feature_value = features.get(spec.feature)
            if (
                isinstance(feature_value, bool)
                or not isinstance(feature_value, (int, float))
                or isinstance(raw_threshold, bool)
                or not isinstance(raw_threshold, (int, float))
            ):
                continue
            distance = abs(float(feature_value) - float(raw_threshold))
            if distance <= margin:
                fragile.append(
                    FragileDecision(
                        index=index,
                        status=_status(record),
                        threshold_name=str(threshold_name),
                        feature=spec.feature,
                        feature_value=float(feature_value),
                        threshold_value=float(raw_threshold),
                        distance=round(distance, 6),
                        direction=spec.direction,
                    )
                )
    return fragile


def _histogram_probabilities(
    values: Sequence[float],
    edges: Sequence[float],
    *,
    epsilon: float = 1e-9,
) -> list[float]:
    bins = [0 for _ in range(max(1, len(edges) - 1))]
    for value in values:
        placed = False
        for index in range(len(edges) - 1):
            left, right = edges[index], edges[index + 1]
            is_last = index == len(edges) - 2
            if left <= value < right or (is_last and value == right):
                bins[index] += 1
                placed = True
                break
        if not placed:
            bins[0 if value < edges[0] else -1] += 1
    total = sum(bins)
    if not total:
        return [0.0 for _ in bins]
    probs = [max(count / total, epsilon) for count in bins]
    norm = sum(probs)
    return [value / norm for value in probs]


def _shared_edges(
    baseline: Sequence[float],
    current: Sequence[float],
    *,
    bins: int = 10,
) -> list[float]:
    combined = sorted([*baseline, *current])
    if not combined:
        return [0.0, 1.0]
    low, high = combined[0], combined[-1]
    if low == high:
        return [low - 0.5, high + 0.5]
    return [
        low + (high - low) * index / bins
        for index in range(bins + 1)
    ]


def population_stability_index(
    baseline: Sequence[float],
    current: Sequence[float],
    *,
    bins: int = 10,
) -> float:
    if not baseline or not current:
        return 0.0
    edges = _shared_edges(baseline, current, bins=bins)
    expected = _histogram_probabilities(baseline, edges)
    actual = _histogram_probabilities(current, edges)
    psi = sum(
        (actual_prob - expected_prob)
        * log(actual_prob / expected_prob)
        for expected_prob, actual_prob in zip(expected, actual)
    )
    return round(float(psi), 6)


def jensen_shannon_divergence(
    baseline: Sequence[float],
    current: Sequence[float],
    *,
    bins: int = 10,
) -> float:
    if not baseline or not current:
        return 0.0
    edges = _shared_edges(baseline, current, bins=bins)
    left = _histogram_probabilities(baseline, edges)
    right = _histogram_probabilities(current, edges)
    middle = [(a + b) / 2.0 for a, b in zip(left, right)]

    def kl(p: Sequence[float], q: Sequence[float]) -> float:
        return sum(
            p_value * log2(p_value / q_value)
            for p_value, q_value in zip(p, q)
            if p_value > 0 and q_value > 0
        )

    return round(0.5 * kl(left, middle) + 0.5 * kl(right, middle), 6)


def kolmogorov_smirnov_statistic(
    baseline: Sequence[float],
    current: Sequence[float],
) -> float:
    if not baseline or not current:
        return 0.0
    left = sorted(baseline)
    right = sorted(current)
    points = sorted(set(left + right))
    left_index = 0
    right_index = 0
    maximum = 0.0
    for point in points:
        while left_index < len(left) and left[left_index] <= point:
            left_index += 1
        while right_index < len(right) and right[right_index] <= point:
            right_index += 1
        maximum = max(
            maximum,
            abs(left_index / len(left) - right_index / len(right)),
        )
    return round(maximum, 6)


def _compare_summary(
    current: RunSummary,
    baseline: RunSummary,
    *,
    rate_delta_limit: float,
    confidence_delta_limit: float,
) -> list[dict[str, object]]:
    alerts: list[dict[str, object]] = []
    statuses = set(current.status_rates) | set(baseline.status_rates)
    for status in sorted(statuses):
        delta = current.status_rates.get(status, 0.0) - baseline.status_rates.get(
            status,
            0.0,
        )
        if abs(delta) >= rate_delta_limit:
            alerts.append(
                {
                    "kind": "status_rate",
                    "status": status,
                    "delta": round(delta, 6),
                }
            )
    if (
        current.mean_confidence is not None
        and baseline.mean_confidence is not None
    ):
        delta = current.mean_confidence - baseline.mean_confidence
        if abs(delta) >= confidence_delta_limit:
            alerts.append(
                {
                    "kind": "mean_confidence",
                    "delta": round(delta, 6),
                }
            )
    return alerts


def compare_to_baseline(
    current_records: Iterable[object],
    baseline_records: Iterable[object],
    *,
    segment_by: Sequence[str] = (),
    rate_delta_limit: float = 0.05,
    confidence_delta_limit: float = 0.05,
    psi_limit: float = 0.20,
    js_limit: float = 0.10,
    ks_limit: float = 0.20,
) -> dict[str, object]:
    current = [_record_to_mapping(record) for record in current_records]
    baseline = [_record_to_mapping(record) for record in baseline_records]
    current_summary = summarize_run(current, segment_by=segment_by)
    baseline_summary = summarize_run(baseline, segment_by=segment_by)

    alerts = _compare_summary(
        current_summary,
        baseline_summary,
        rate_delta_limit=rate_delta_limit,
        confidence_delta_limit=confidence_delta_limit,
    )

    current_features = _numeric_feature_values(current)
    baseline_features = _numeric_feature_values(baseline)
    feature_drift: dict[str, dict[str, float]] = {}
    for feature in sorted(set(current_features) & set(baseline_features)):
        current_values = current_features[feature]
        baseline_values = baseline_features[feature]
        if len(current_values) < 2 or len(baseline_values) < 2:
            continue
        metrics = {
            "psi": population_stability_index(
                baseline_values,
                current_values,
            ),
            "jensen_shannon": jensen_shannon_divergence(
                baseline_values,
                current_values,
            ),
            "ks": kolmogorov_smirnov_statistic(
                baseline_values,
                current_values,
            ),
        }
        feature_drift[feature] = metrics
        if (
            metrics["psi"] >= psi_limit
            or metrics["jensen_shannon"] >= js_limit
            or metrics["ks"] >= ks_limit
        ):
            alerts.append(
                {
                    "kind": "feature_drift",
                    "feature": feature,
                    **metrics,
                }
            )

    segment_alerts: dict[str, list[dict[str, object]]] = {}
    if segment_by:
        all_segments = set(current_summary.segments) | set(
            baseline_summary.segments
        )
        for segment in sorted(all_segments):
            current_segment = current_summary.segments.get(segment)
            baseline_segment = baseline_summary.segments.get(segment)
            if current_segment is None or baseline_segment is None:
                segment_alerts[segment] = [
                    {
                        "kind": "segment_presence",
                        "current": current_segment is not None,
                        "baseline": baseline_segment is not None,
                    }
                ]
                continue
            found = _compare_summary(
                current_segment,
                baseline_segment,
                rate_delta_limit=rate_delta_limit,
                confidence_delta_limit=confidence_delta_limit,
            )
            if found:
                segment_alerts[segment] = found

    return {
        "current": current_summary.as_dict(),
        "baseline": baseline_summary.as_dict(),
        "feature_drift": feature_drift,
        "alerts": alerts,
        "segment_alerts": segment_alerts,
    }


def sweep_binary_threshold(
    records: Iterable[object],
    *,
    feature: str,
    truth_field: str,
    thresholds: Sequence[float],
) -> list[dict[str, float | int]]:
    """Evaluate a candidate minimum threshold against human ground truth."""
    mapped = [_record_to_mapping(record) for record in records]
    output: list[dict[str, float | int]] = []
    for threshold in thresholds:
        tp = fp = fn = tn = 0
        for record in mapped:
            value = _features(record).get(feature)
            truth = record.get(truth_field)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isinstance(truth, bool)
            ):
                continue
            predicted = float(value) >= threshold
            if predicted and truth:
                tp += 1
            elif predicted and not truth:
                fp += 1
            elif not predicted and truth:
                fn += 1
            else:
                tn += 1
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
        output.append(
            {
                "threshold": round(float(threshold), 6),
                "precision": round(precision, 6),
                "recall": round(recall, 6),
                "f1": round(f1, 6),
                "false_positive_rate": round(
                    fp / (fp + tn) if fp + tn else 0.0,
                    6,
                ),
                "false_negative_rate": round(
                    fn / (fn + tp) if fn + tp else 0.0,
                    6,
                ),
                "tp": tp,
                "fp": fp,
                "fn": fn,
                "tn": tn,
            }
        )
    return output


def choose_precision_first_threshold(
    sweep: Sequence[Mapping[str, float | int]],
    *,
    min_precision: float = 0.99,
) -> Mapping[str, float | int] | None:
    eligible = [
        row
        for row in sweep
        if float(row.get("precision", 0.0)) >= min_precision
    ]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda row: (
            float(row.get("recall", 0.0)),
            float(row.get("f1", 0.0)),
            -float(row.get("threshold", 0.0)),
        ),
    )
