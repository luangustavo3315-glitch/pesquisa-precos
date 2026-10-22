from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable

from .models import Product, Candidate
from .normalizer import normalize_text, quantity_signature

FLEXIBLE_VARIANT_FAMILIES = (
    "macarrao instantaneo",
    "refresco em po",
    "molho de tomate",
    "bebida lactea",
    "achocolatado",
    "biscoito",
    "bombom",
    "chocolate",
    "gelatina",
    "iogurte",
    "sorvete",
    "tempero",
)

SUBSTITUTABLE_PRODUCT_TYPES = (
    "creme de tratamento",
    "detergente em po",
    "detergente liquido",
    "condicionador",
    "desinfetante",
    "amaciante",
    "limpador",
    "sabonete",
    "shampoo",
    "desodorante aerossol",
    "desodorante roll on",
)

SPECIES_GROUPS = {
    "bovino": {"bovino", "bovina", "boi"},
    "suino": {"suino", "suina", "porco", "porcina", "porcino"},
    "frango": {"frango", "aves", "ave"},
}
GENDER_GROUPS = {
    "masculino": {"men", "masculino", "masculina", "masc"},
    "feminino": {"women", "woman", "feminino", "feminina", "fem"},
}

SEMANTIC_EQUIVALENCES = (
    ("suco em po", "refresco em po"),
    ("leite uht", "leite longa vida"),
    ("sabao em po", "detergente em po"),
)

PACKAGING_GROUPS = {
    "caixa": {"caixa", "cx", "tetra pak", "tp"},
    "sache": {"sache", "sachet", "pouch", "refil"},
    "pote": {"pote"},
    "lata": {"lata"},
    "pet": {"pet", "garrafa pet"},
    "almofada": {"almofada"},
}
PACKAGING_TOKENS = {token for values in PACKAGING_GROUPS.values() for token in values}
FRESH_DISTINCTIVE_MODIFIERS = {
    "baby", "mini", "organico", "organica", "hidroponico", "hidroponica",
    "roxo", "roxa", "premium", "gourmet",
}
SPECIAL_VARIANT_PHRASES = {
    "integral", "semidesnatado", "desnatado", "light", "diet",
    "zero", "picante", "empanado", "empanada", "temperado", "temperada",
    "zero lactose", "sem lactose", "zero acucar", "sem acucar",
    "sem gluten", "sem sal", "organico", "organica", "baby", "mini",
    "premium", "gourmet",
}
BUNDLE_MARKERS = {"kit", "combo", "pack", "multipack"}
WEIGHTED_PRESENTATION_WORDS = {
    "fresco", "fresca", "resfriado", "resfriada", "congelado", "congelada",
    "nacional", "importado", "importada", "grn", "granel", "inteiro", "inteira",
}

# Palavras que transformam uma fruta/verdura in natura em outro tipo de produto.
# A regra é deliberadamente por domínio, não por SKU: impede que um termo do
# alvo usado como sabor (ex.: "morango") torne uma cobertura/sorvete um match.
FRESH_PROCESSED_PRODUCT_TERMS = {
    "aromatizador", "aroma", "bala", "bebida", "biscoito", "bolo", "calda",
    "chocolate", "cobertura", "compota", "conserva", "doce", "essencia",
    "geleia", "iogurte", "molho", "nectar", "polpa", "refresco", "recheio",
    "sorvete", "suco", "tempero", "xarope",
}

# Estados/processamentos que mudam a natureza de um corte de carne/peixe cru.
# Congelado/resfriado ficam de fora: são apresentação/conservação e não outro
# produto. Se o próprio alvo pedir o processamento (ex.: "temperado"), ele
# continua permitido.
RAW_PROTEIN_PROCESSING_TERMS = {
    "assado", "assada", "cozido", "cozida", "defumado", "defumada",
    "empanado", "empanada", "hamburguer", "linguica", "marinado", "marinada",
    "mortadela", "nugget", "nuggets", "presunto", "processado", "processada",
    "recheado", "recheada", "salame", "salsicha", "temperado", "temperada",
}


@lru_cache(maxsize=16_384)
def _semantic_text_cached(text: str) -> str:
    n = normalize_text(text)
    for source, canonical in SEMANTIC_EQUIVALENCES:
        n = n.replace(source, canonical)
    return " ".join(n.split())


def semantic_text(text: str) -> str:
    return _semantic_text_cached(str(text or ""))


def term_present(term: str, haystack: str) -> bool:
    t = semantic_text(term)
    h = semantic_text(haystack)
    if not t:
        return False
    if f" {t} " in f" {h} ":
        return True
    tokens = t.split()
    haystack_tokens = set(h.split())
    return len(tokens) > 1 and all(token in haystack_tokens for token in tokens)


def brand_present(brand: str, evidence: str) -> bool:
    brand_n = normalize_text(brand)
    evidence_n = normalize_text(evidence)
    if not brand_n:
        return True
    if f" {brand_n} " in f" {evidence_n} ":
        return True

    brand_tokens = brand_n.split()
    evidence_tokens = evidence_n.split()
    brand_compact = re.sub(r"[^a-z0-9]", "", brand_n)
    compact_windows: set[str] = set(evidence_tokens)
    max_window = min(max(len(brand_tokens), 2), 4)
    for width in range(2, max_window + 1):
        for i in range(0, len(evidence_tokens) - width + 1):
            compact_windows.add("".join(evidence_tokens[i:i + width]))

    forms = {brand_compact}
    if brand_n.startswith("dr "):
        forms.add(re.sub(r"[^a-z0-9]", "", brand_n[3:]))
    return any(form and form in compact_windows for form in forms)


def detect_product_type(text: str) -> str:
    normalized = normalize_text(text)
    return next(
        (product_type for product_type in SUBSTITUTABLE_PRODUCT_TYPES if normalize_text(product_type) in normalized),
        "",
    )


def same_product_type(target: str, evidence: str) -> tuple[bool, str]:
    target_n = normalize_text(target)
    evidence_n = normalize_text(evidence)
    for product_type in SUBSTITUTABLE_PRODUCT_TYPES:
        product_type_n = normalize_text(product_type)
        if product_type_n not in target_n:
            continue
        if product_type == "desodorante aerossol":
            has_type = "desodorante" in evidence_n and "aerossol" in evidence_n
            has_conflict = any(x in evidence_n for x in ("roll on", "rollon", "stick"))
            return has_type and not has_conflict, product_type
        if product_type == "desodorante roll on":
            has_type = "desodorante" in evidence_n and ("roll on" in evidence_n or "rollon" in evidence_n)
            has_conflict = any(x in evidence_n for x in ("aerossol", "aerosol", "spray", "stick"))
            return has_type and not has_conflict, product_type
        if product_type == "detergente liquido":
            has_liquid_measure = "volume_ml" in quantity_signature(evidence)
            is_liquid_detergent = "detergente" in evidence_n and " em po " not in f" {evidence_n} "
            return has_liquid_measure and is_liquid_detergent, product_type
        if product_type == "detergente em po":
            is_powder = any(
                phrase in evidence_n
                for phrase in ("detergente em po", "sabao em po", "lava roupas em po")
            )
            return is_powder, product_type
        return product_type_n in evidence_n, product_type
    return False, ""



def flexible_variant_family(text: str) -> str:
    """Return a broad family in which flavor/version substitution is allowed.

    Word order is irrelevant because ``normalize_text`` is used and matching is
    based on phrase/tokens. Protected nutritional, packaging, bundle, species
    and unit differences are still rejected earlier by the normal guards.
    """
    normalized = normalize_text(text)
    for family in FLEXIBLE_VARIANT_FAMILIES:
        family_n = normalize_text(family)
        if family_n in normalized:
            return family
    return ""


def same_flexible_variant_family(target: str, evidence: str) -> tuple[bool, str]:
    family = flexible_variant_family(target)
    if not family:
        return False, ""
    family_n = normalize_text(family)
    return family_n in normalize_text(evidence), family


def same_explicit_quantity(target: str, evidence: str) -> bool:
    expected = quantity_signature(target)
    found = quantity_signature(evidence)
    if not expected:
        return False
    return all(kind in found and values.issubset(found[kind]) for kind, values in expected.items())


def core_weighted_name(text: str) -> str:
    n = normalize_text(text)
    n = re.sub(r"\bkg\b", " ", n)
    n = re.sub(r"\b(?:por kg|quilo|quilograma)\b", " ", n)
    return " ".join(n.split())


def required_term_present(term: str, evidence: str) -> bool:
    return term_present(term, evidence)


def fresh_modifier_guard(target: str, evidence: str) -> tuple[bool, str]:
    target_tokens = set(normalize_text(target).split())
    evidence_tokens = set(normalize_text(evidence).split())
    extra = (evidence_tokens - target_tokens).intersection(FRESH_DISTINCTIVE_MODIFIERS)
    if extra:
        return False, f"variante fresca específica não solicitada: {sorted(extra)}"
    return True, ""


def weighted_generic_extra_guard(target: str, candidate_title: str) -> tuple[bool, str]:
    core_target = core_weighted_name(target)
    core_candidate = core_weighted_name(candidate_title)
    target_tokens = {
        t for t in normalize_text(core_target).split()
        if len(t) > 2 and not re.fullmatch(r"\d+(?:\.\d+)?", t)
    }
    candidate_tokens = {
        t for t in normalize_text(core_candidate).split()
        if len(t) > 2 and not re.fullmatch(r"\d+(?:\.\d+)?", t)
    }
    if len(target_tokens) != 1:
        return True, ""
    extras = candidate_tokens - target_tokens - WEIGHTED_PRESENTATION_WORDS
    if extras:
        return False, f"subtipo/corte adicional não solicitado: {sorted(extras)}"
    return True, ""


def _category_semantic_guard(
    product: Product, candidate: Candidate, evidence: str, store_key: str
) -> tuple[bool, str]:
    """Reject a candidate whose *name* changes the semantic product domain.

    Matching used to reward target containment too strongly (RapidFuzz
    token_set_ratio): ``Morango 200 g`` was a perfect subset of
    ``Cobertura ... Morango 200 g`` and could therefore become EXATO.  This
    guard intentionally evaluates only the canonical candidate title, not
    descriptions/recommendations, and treats words that turn a raw item into a
    processed product as negative evidence.
    """
    del store_key  # reserved for future store-specific vocabulary
    title = semantic_text(candidate.title or evidence)
    target = semantic_text(product.search_name)
    category = normalize_text(product.categoria)
    target_tokens = set(target.split())
    title_tokens = set(title.split())

    if category == "hortifruti":
        extras = (title_tokens & FRESH_PROCESSED_PRODUCT_TERMS) - (
            target_tokens & FRESH_PROCESSED_PRODUCT_TERMS
        )
        if extras:
            return False, (
                "nome do candidato muda o produto de hortifruti para item processado: "
                f"{sorted(extras)}"
            )

    if category in {"carnes", "peixaria"}:
        extras = (title_tokens & RAW_PROTEIN_PROCESSING_TERMS) - (
            target_tokens & RAW_PROTEIN_PROCESSING_TERMS
        )
        if extras:
            return False, (
                "processamento adicional não solicitado no corte/produto: "
                f"{sorted(extras)}"
            )
    return True, ""


def _packaging_labels(text: str) -> set[str]:
    n = normalize_text(text)
    labels: set[str] = set()
    for label, terms in PACKAGING_GROUPS.items():
        if any(term_present(term, n) for term in terms):
            labels.add(label)
    return labels


def _packaging_guard(product: Product, candidate: Candidate, evidence: str, store_key: str) -> tuple[bool, str]:
    expected = _packaging_labels(product.search_name)
    found = _packaging_labels(evidence)
    if expected and found and expected.isdisjoint(found):
        return False, f"embalagem divergente (esperado {sorted(expected)}, encontrado {sorted(found)})"
    return True, ""


def _special_variant_guard(product: Product, candidate: Candidate, evidence: str, store_key: str) -> tuple[bool, str]:
    target_n = semantic_text(product.search_name)
    evidence_n = semantic_text(evidence)
    target_variants = {v for v in SPECIAL_VARIANT_PHRASES if term_present(v, target_n)}
    found_variants = {v for v in SPECIAL_VARIANT_PHRASES if term_present(v, evidence_n)}
    extras = found_variants - target_variants
    if extras:
        return False, f"variante especial não solicitada: {sorted(extras)}"

    milk_group = {"integral", "semidesnatado", "desnatado"}
    expected_milk = target_variants.intersection(milk_group)
    found_milk = found_variants.intersection(milk_group)
    if expected_milk and found_milk and expected_milk != found_milk:
        return False, (
            f"variante nutricional divergente (esperado {sorted(expected_milk)}, "
            f"encontrado {sorted(found_milk)})"
        )
    return True, ""


def _bundle_guard(product: Product, candidate: Candidate, evidence: str, store_key: str) -> tuple[bool, str]:
    candidate_title = candidate.title or evidence
    target_n = normalize_text(product.search_name)
    title_n = normalize_text(candidate_title)
    target_tokens = set(target_n.split())
    title_tokens = set(title_n.split())

    extra_bundle = title_tokens.intersection(BUNDLE_MARKERS) - target_tokens.intersection(BUNDLE_MARKERS)
    if extra_bundle:
        return False, f"kit/multipack não solicitado: {sorted(extra_bundle)}"

    expected_counts = quantity_signature(product.search_name).get("count", set())
    title_counts = quantity_signature(candidate_title).get("count", set())
    if not expected_counts and any(value > 1 for value in title_counts):
        return False, f"embalagem múltipla não solicitada: {sorted(title_counts)} unidades"
    return True, ""


def _unit_guard(product: Product, candidate: Candidate, evidence: str, store_key: str) -> tuple[bool, str]:
    if product.unidade_preco == "un":
        if candidate.unit_price == "kg":
            return False, "preço do candidato é por kg, mas o item solicitado é por unidade/embalagem"
        if candidate.unit_price in {"fraction", "kg_labeled", "ambiguous"}:
            return False, "base do preço do candidato não confirma a embalagem solicitada por unidade"
        return True, ""

    if candidate.unit_price == "kg":
        return True, ""
    if candidate.unit_price == "un":
        return False, "preço do candidato é por unidade/embalagem, mas o item solicitado é por kg"
    if candidate.unit_price == "fraction":
        return False, "o catálogo mostrou preço de uma fração/embalagem, não um preço direto por kg"
    if candidate.unit_price == "kg_labeled":
        return False, "rótulo por kg ainda não foi validado contra o próprio cartão"
    if candidate.unit_price == "ambiguous":
        return False, "preço por kg ausente ou contraditório no cartão"
    return False, "unidade de venda por kg não informada diretamente pelo catálogo"


def _species_groups(text: str) -> set[str]:
    tokens = set(normalize_text(text).split())
    groups: set[str] = set()
    for group, terms in SPECIES_GROUPS.items():
        if tokens.intersection(terms):
            groups.add(group)
    return groups


def _species_guard(product: Product, candidate: Candidate, evidence: str, store_key: str) -> tuple[bool, str]:
    expected_text = " ".join([
        product.search_name,
        *product.aliases_for_store(store_key),
        *product.termos_obrigatorios,
    ])
    expected = _species_groups(expected_text)
    found = _species_groups(evidence)
    if expected and found and expected.isdisjoint(found):
        return False, f"espécie incompatível (esperado {sorted(expected)}, encontrado {sorted(found)})"
    return True, ""


def _gender_groups(text: str) -> set[str]:
    tokens = set(normalize_text(text).split())
    groups: set[str] = set()
    for group, terms in GENDER_GROUPS.items():
        if tokens.intersection(terms):
            groups.add(group)
    return groups


def _gender_guard(product: Product, candidate: Candidate, evidence: str, store_key: str) -> tuple[bool, str]:
    expected_text = " ".join([
        product.search_name,
        *product.aliases_for_store(store_key),
        *product.termos_obrigatorios,
    ])
    expected = _gender_groups(expected_text)
    found = _gender_groups(evidence)
    if expected and found and expected.isdisjoint(found):
        return False, f"público/gênero incompatível (esperado {sorted(expected)}, encontrado {sorted(found)})"
    return True, ""


@dataclass(frozen=True)
class GuardFailure:
    name: str
    confidence: float
    reason: str


GuardFunction = Callable[[Product, Candidate, str, str], tuple[bool, str]]

# Ordem importa: erros estruturais mais fortes são avaliados antes de scoring.
PRECLASSIFICATION_GUARDS: tuple[tuple[str, float, GuardFunction], ...] = (
    ("bundle", 0.17, _bundle_guard),
    ("special_variant", 0.16, _special_variant_guard),
    ("semantic_domain", 0.14, _category_semantic_guard),
    ("unit", 0.20, _unit_guard),
    ("packaging", 0.22, _packaging_guard),
    ("species", 0.18, _species_guard),
    ("gender", 0.19, _gender_guard),
)


def run_preclassification_guards(
    product: Product,
    candidate: Candidate,
    evidence: str,
    store_key: str,
) -> GuardFailure | None:
    for name, confidence, guard in PRECLASSIFICATION_GUARDS:
        ok, reason = guard(product, candidate, evidence, store_key)
        if not ok:
            return GuardFailure(name=name, confidence=confidence, reason=reason)
    return None
