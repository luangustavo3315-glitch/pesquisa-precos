from __future__ import annotations

"""Parsing and validation of monetary evidence.

This module is the single source of truth for how a price moves from raw page
text to an eligible value. Collectors may discover candidates in different
ways, but neither the report nor the history layer re-implements price rules.
"""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .normalizer import quantity_signature

if TYPE_CHECKING:
    from .models import Candidate, Product

VALID_SOURCE_STATUSES = {"EXATO", "SUBSTITUTO ACEITO"}
UNSAFE_PRICE_UNITS = {"fraction", "kg_labeled", "ambiguous"}
PRICE_EVIDENCE_SCHEMA = 1

_MONEY = r"[0-9]{1,5}(?:[.\s][0-9]{3})*(?:,[0-9]{2})?"
PRICE_RE = re.compile(rf"R\$\s*({_MONEY})", re.I)
DE_POR_RE = re.compile(
    r"de\s*R\$\s*([0-9\.]+,[0-9]{2}).{0,60}?(?:por|agora)\s*R\$\s*([0-9\.]+,[0-9]{2})",
    re.I | re.S,
)
LEVE_PAGUE_RE = re.compile(
    r"leve\s+(\d+)\s+(?:e\s+)?(?:pague|por)\s+R\$\s*([0-9\.]+,[0-9]{2})\s*(?:cada)?",
    re.I,
)
AVERAGE_WEIGHT_PATTERNS = (
    re.compile(r"m[eé]dia\s+da\s+unidade\s*:\s*([0-9]+(?:[.,][0-9]+)?)\s*kg", re.I),
    re.compile(r"(?:1\s+unidade\s+)?aproximadamente\s*([0-9]+(?:[.,][0-9]+)?)\s*kg", re.I),
)
EACH_WEIGHT_PRICE_RE = re.compile(
    rf"R\$\s*({_MONEY})\s*a\s+cada\s+([0-9]+(?:[.,][0-9]+)?)\s*kg",
    re.I,
)
WEIGHT_EQUALS_PRICE_RE = re.compile(
    rf"([0-9]+(?:[.,][0-9]+)?)\s*kg\s*=\s*R\$\s*({_MONEY})",
    re.I,
)
DIRECT_PER_KG_RE = re.compile(
    rf"R\$\s*({_MONEY})\s*(?:/\s*kg|por\s+kg)",
    re.I,
)
LABELED_PER_KG_RE = re.compile(
    rf"pre[cç]o\s+por\s+kg\s*:\s*R\$\s*({_MONEY})",
    re.I,
)
WEIGHT_PRICE_BASIS_RE = re.compile(
    r"(?:/\s*|por\s+)(\d+(?:[.,]\d+)?)\s*(kg|g|gramas?|quilos?)"
    r"(?=\s|cada|comprar|$)",
    re.I,
)
SAVINGS_MONEY_RE = re.compile(
    r"(?:economize|economia\s+de|cashback(?:\s+de)?|ganhe|cupom(?:\s+de)?|desconto(?:\s+de)?)"
    r"\s*R\$\s*[0-9]{1,5}(?:[.\s][0-9]{3})*(?:,[0-9]{2})?",
    re.I,
)
INSTALLMENT_RE = re.compile(
    r"\b\d+\s*x\s+(?:de\s+)?R\$\s*[0-9]{1,5}(?:[.\s][0-9]{3})*(?:,[0-9]{2})?",
    re.I,
)
DYNAMIC_LEVE_PAGUE_RE = re.compile(r"\bleve\s+(\d+)\s+pague\s+(\d+)\b", re.I)


@dataclass(frozen=True)
class PriceEvidence:
    """Structured proof for the monetary value selected by the system."""

    basis: str = ""  # unit, direct_package, direct_kg, fraction, ambiguous
    validation: str = ""  # verified, ambiguous, rejected
    raw_label: str = ""
    value: float | None = None
    source_url: str = ""
    captured_at: str = ""
    schema: int = PRICE_EVIDENCE_SCHEMA


def _value(result: Any, key: str, default=None):
    if isinstance(result, dict):
        return result.get(key, default)
    return getattr(result, key, default)


def money_to_float(value: str) -> float:
    return float(value.replace(".", "").replace(" ", "").replace(",", "."))


def decimal_to_float(value: str) -> float:
    return float(str(value).strip().replace(",", "."))


def extract_average_weight_kg(text: str) -> float | None:
    text = str(text or "")
    for pattern in AVERAGE_WEIGHT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        try:
            value = decimal_to_float(match.group(1))
        except ValueError:
            continue
        if 0 < value <= 100:
            return value
    match = EACH_WEIGHT_PRICE_RE.search(text)
    if match:
        try:
            value = decimal_to_float(match.group(2))
            if 0 < value <= 100:
                return value
        except ValueError:
            pass
    match = WEIGHT_EQUALS_PRICE_RE.search(text)
    if match:
        try:
            value = decimal_to_float(match.group(1))
            if 0 < value <= 100:
                return value
        except ValueError:
            pass
    return None


def extract_sale_weight_basis_kg(text: str) -> float | None:
    match = WEIGHT_PRICE_BASIS_RE.search(str(text or ""))
    if not match:
        return None
    try:
        value = decimal_to_float(match.group(1))
    except ValueError:
        return None
    unit = match.group(2).lower()
    kilograms = value if unit.startswith(("kg", "quilo")) else value / 1000.0
    return kilograms if 0 < kilograms <= 100 else None


def extract_labeled_per_kg_price(text: str) -> float | None:
    match = LABELED_PER_KG_RE.search(str(text or ""))
    if not match:
        return None
    try:
        value = money_to_float(match.group(1))
    except ValueError:
        return None
    return value if 0.01 <= value <= 100000 else None



def extract_inline_per_kg_prices(text: str) -> list[float]:
    values: list[float] = []
    for match in DIRECT_PER_KG_RE.finditer(str(text or "")):
        try:
            value = money_to_float(match.group(1))
        except ValueError:
            continue
        if 0.01 <= value <= 100000 and value not in values:
            values.append(value)
    return values

def extract_direct_per_kg_prices(text: str) -> list[float]:
    values: list[float] = []
    # Both grammars are accepted; de-duplication is intentional.
    for pattern in (DIRECT_PER_KG_RE, LABELED_PER_KG_RE):
        for match in pattern.finditer(str(text or "")):
            try:
                value = money_to_float(match.group(1))
            except ValueError:
                continue
            if 0.01 <= value <= 100000 and value not in values:
                values.append(value)
    return values


def extract_direct_per_kg_labels(text: str) -> list[str]:
    labels: list[str] = []
    for pattern in (DIRECT_PER_KG_RE, LABELED_PER_KG_RE):
        for match in pattern.finditer(str(text or "")):
            label = match.group(0).strip()
            if label and label not in labels:
                labels.append(label)
    return labels


def _fraction_price_for_validation(text: str, fallback: float | None) -> float | None:
    match = EACH_WEIGHT_PRICE_RE.search(str(text or ""))
    if match:
        try:
            return money_to_float(match.group(1))
        except ValueError:
            pass
    match = WEIGHT_EQUALS_PRICE_RE.search(str(text or ""))
    if match:
        try:
            return money_to_float(match.group(2))
        except ValueError:
            pass
    return float(fallback) if isinstance(fallback, (int, float)) else None


def detect_sale_unit(text: str) -> str:
    n = str(text or "").lower()
    if re.search(r"r\$\s*[0-9.,]+\s*(?:/\s*kg|por\s+kg\b)|/\s*kg\b", n, re.I):
        return "kg"
    if LABELED_PER_KG_RE.search(n):
        return "kg_labeled"
    if WEIGHT_PRICE_BASIS_RE.search(n):
        return "fraction"
    if re.search(r"/\s*un\b|por\s+unidade\b|/\s*unidade\b", n, re.I):
        return "un"
    return ""


def parse_prices(text: str) -> tuple[float | None, float | None, float | None, str]:
    """Parse prices while preserving the user's rule: cheapest valid offer wins.

    Club/CPF/Pix/coupon prices are not separated out of the operational result;
    their condition is recorded, but the lowest displayed product price remains
    eligible once product/unit evidence is valid.
    """
    if not text:
        return None, None, None, ""
    text = re.sub(
        r"frete\s+gr[aá]tis\s+acima\s+(?:de\s+)?r\$\s*[0-9.,]+",
        " ",
        text,
        flags=re.I,
    )
    m = DE_POR_RE.search(text)
    if m:
        normal, promo = money_to_float(m.group(1)), money_to_float(m.group(2))
        return normal, promo, min(normal, promo), f"de R$ {m.group(1)} por R$ {m.group(2)}"

    text_for_prices = SAVINGS_MONEY_RE.sub(" ", text)
    text_for_prices = INSTALLMENT_RE.sub(" ", text_for_prices)
    labeled_per_kg = extract_labeled_per_kg_price(text_for_prices)
    text_for_prices = LABELED_PER_KG_RE.sub(" ", text_for_prices)

    vals: list[float] = []
    for raw in PRICE_RE.findall(text_for_prices):
        try:
            value = money_to_float(raw)
        except ValueError:
            continue
        if 0.01 <= value <= 100000 and value not in vals:
            vals.append(value)

    leve_pague = LEVE_PAGUE_RE.search(text_for_prices)
    if leve_pague and vals:
        promo = money_to_float(leve_pague.group(2))
        return vals[0], promo, min(vals[0], promo), f"leve {leve_pague.group(1)}, R$ {leve_pague.group(2)} cada"

    discount_label = re.search(r"\b\d{1,3}\s*%\s*off\b|desconto", text, re.I)
    if len(vals) >= 2 and (discount_label or vals[1] < vals[0]):
        normal, promo = max(vals[0], vals[1]), min(vals[0], vals[1])
        lower = text.lower()
        if "pix" in lower:
            condition = "preço promocional via Pix"
        elif any(token in lower for token in ("clube", "club ", "cpf", "fidelidade")):
            condition = "preço promocional condicionado a clube/CPF"
        elif "cupom" in lower:
            condition = "preço promocional com cupom"
        else:
            condition = "preço promocional exibido na listagem" if discount_label else "preço reduzido exibido na listagem"
        return normal, promo, promo, condition

    observed = vals[0] if vals else None
    if observed is None and labeled_per_kg is not None and extract_average_weight_kg(text) is None:
        observed = labeled_per_kg
        return observed, None, observed, "preço por kg explícito do catálogo"
    condition = ""
    dynamic = DYNAMIC_LEVE_PAGUE_RE.search(text)
    if dynamic:
        condition = f"leve {dynamic.group(1)} pague {dynamic.group(2)}; valor unitário exibido sem rateio automático"
    return observed, None, observed, condition


def _set_evidence(candidate: Candidate, evidence: PriceEvidence) -> None:
    candidate.price_basis = evidence.basis
    candidate.price_validation = evidence.validation
    candidate.raw_price_label = evidence.raw_label
    candidate.price_evidence_schema = evidence.schema
    candidate.price_evidence_value = (
        float(evidence.value) if isinstance(evidence.value, (int, float))
        else float(candidate.price_observed) if isinstance(candidate.price_observed, (int, float))
        else None
    )
    candidate.price_evidence_source_url = evidence.source_url or candidate.url


def normalize_weighted_candidate(product: Product, candidate: Candidate) -> Candidate:
    """Normalize only when direct evidence exists; never derive R$/kg."""
    if candidate.weighted_price_normalized:
        return candidate

    if product.unidade_preco == "un" and candidate.unit_price == "fraction":
        target_mass = quantity_signature(product.search_name).get("mass_g", set())
        title_mass = quantity_signature(candidate.title).get("mass_g", set())
        if len(target_mass) == 1 and target_mass == title_mass:
            grams = next(iter(target_mass))
            basis_kg = extract_sale_weight_basis_kg(candidate.raw_text)
            if grams > 0 and basis_kg is not None and abs((basis_kg * 1000.0) - grams) < 0.01:
                candidate.unit_price = "un"
                candidate.weighted_price_normalized = True
                candidate.price_normalization_note = (
                    f"preço direto da embalagem fixa de {grams:g} g; nenhum cálculo aplicado"
                )
                _set_evidence(candidate, PriceEvidence("direct_package", "verified", f"/{grams:g}g"))
                return candidate
        _set_evidence(candidate, PriceEvidence("fraction", "ambiguous", ""))
        return candidate

    if product.unidade_preco != "kg":
        if candidate.price_observed is not None and candidate.unit_price in {"", "un"}:
            candidate.unit_price = candidate.unit_price or "un"
            _set_evidence(candidate, PriceEvidence("unit", "verified", ""))
        elif candidate.unit_price in UNSAFE_PRICE_UNITS:
            _set_evidence(candidate, PriceEvidence(candidate.unit_price, "ambiguous", ""))
        return candidate

    text = candidate.raw_text or ""
    direct_per_kg = extract_inline_per_kg_prices(text)
    if direct_per_kg:
        observed = min(direct_per_kg)
        candidate.price_normal = max(direct_per_kg)
        candidate.price_promo = observed if len(direct_per_kg) > 1 and observed < max(direct_per_kg) else None
        candidate.price_observed = observed
        candidate.unit_price = "kg"
        candidate.weighted_price_normalized = True
        candidate.price_normalization_note = "preço R$/kg informado diretamente pelo catálogo; nenhum cálculo aplicado"
        _set_evidence(
            candidate,
            PriceEvidence("direct_kg", "verified", " | ".join(extract_direct_per_kg_labels(text))),
        )
        return candidate

    labeled_per_kg = extract_labeled_per_kg_price(text)
    if labeled_per_kg is not None:
        label_match = LABELED_PER_KG_RE.search(text)
        average_kg = extract_average_weight_kg(text)
        fraction_price = _fraction_price_for_validation(text, candidate.price_observed)
        if average_kg is not None and fraction_price is not None and abs((labeled_per_kg * average_kg) - fraction_price) > 0.02:
            candidate.unit_price = "ambiguous"
            candidate.weighted_price_normalized = True
            candidate.price_normalization_note = "rótulo de R$/kg rejeitado por contradizer o preço da fração; nenhum cálculo aplicado"
            _set_evidence(
                candidate,
                PriceEvidence("ambiguous", "rejected", label_match.group(0) if label_match else ""),
            )
            return candidate
        candidate.price_normal = labeled_per_kg
        candidate.price_promo = None
        candidate.price_observed = labeled_per_kg
        candidate.unit_price = "kg"
        candidate.weighted_price_normalized = True
        candidate.price_normalization_note = "preço R$/kg explicitamente rotulado e validado; nenhum cálculo aplicado"
        _set_evidence(candidate, PriceEvidence("direct_kg", "verified", label_match.group(0) if label_match else ""))
        return candidate

    candidate.unit_price = "fraction" if (extract_sale_weight_basis_kg(text) is not None or extract_average_weight_kg(text) is not None) else "ambiguous"
    candidate.weighted_price_normalized = True
    candidate.price_normalization_note = "preço por kg não informado diretamente; preço fracionado não foi convertido"
    _set_evidence(candidate, PriceEvidence(candidate.unit_price, "ambiguous", ""))
    return candidate


def eligible_price(result: Any) -> float | str:
    """Return the cheapest valid product price using structured evidence.

    The condition (club/CPF/Pix/coupon) is intentionally *not* a reason to
    discard a lower price. This preserves the project's business rule.
    """
    status = str(_value(result, "status", "") or "")
    if status not in {"EXATO", "SUBSTITUTO ACEITO", "PROVÁVEL"}:
        return "X"
    source_status = str(_value(result, "source_match_status", "") or status)
    if source_status not in VALID_SOURCE_STATUSES:
        return "X"

    unit_price = str(_value(result, "unit_price", "") or "").lower()
    if unit_price in UNSAFE_PRICE_UNITS:
        return "X"

    basis = str(_value(result, "price_basis", "") or "")
    validation = str(_value(result, "price_validation", "") or "")
    raw_text = str(_value(result, "raw_text", "") or "")

    if validation in {"ambiguous", "rejected"}:
        return "X"

    direct_kg_values: list[float] = []
    if unit_price == "kg":
        if validation == "verified":
            if basis != "direct_kg":
                return "X"
            raw_label = str(_value(result, "raw_price_label", "") or "")
            direct_kg_values = extract_direct_per_kg_prices(raw_text)
            if not raw_label and not direct_kg_values:
                return "X"
        else:
            # Backward compatibility for older JSON: raw page evidence is
            # enough; human-readable notes are never used as authorization.
            direct_kg_values = extract_direct_per_kg_prices(raw_text)
            if not direct_kg_values:
                return "X"

    numeric: list[float] = []
    for key in ("definitive_price", "price_observed", "price_promo", "price_normal"):
        value = _value(result, key)
        if not isinstance(value, (int, float)):
            continue
        value = float(value)
        if unit_price == "kg" and direct_kg_values and not any(abs(value - evidence) <= 0.011 for evidence in direct_kg_values):
            continue
        if value > 0 and value not in numeric:
            numeric.append(value)
    if not numeric:
        return "X"
    return round(min(numeric), 2)
