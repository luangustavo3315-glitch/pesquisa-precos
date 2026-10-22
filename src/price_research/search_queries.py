from __future__ import annotations

import re

from .matcher import detect_product_type
from .models import Product
from .normalizer import extract_quantities, normalize_text

GENERIC_TERMS = {
    "aerossol", "amaciante", "condicionador", "creme dental", "desinfetante",
    "detergente", "inseticida", "limpador", "po", "roll on", "sabonete", "shampoo",
}

FRESH_CATEGORIES = {"hortifruti", "carnes", "peixaria", "padaria"}

# Palavras de apresentação que normalmente não ajudam a encontrar o item no
# campo de busca do supermercado. Elas continuam sendo validadas no matcher.
SEARCH_NOISE = {
    "kg", "unidade", "unidades", "fatiado", "fatiada", "peca", "pacote", "pct",
    "a vacuo", "vacuo",
}

SEARCH_SYNONYMS = (
    ("refresco em po", "suco em po"),
    ("leite longa vida", "leite uht"),
    ("detergente em po", "sabao em po"),
    ("pao forma", "pao de forma"),
    ("la de aco", "esponja de aco"),
)


def _quantity_label(kind: str, value: float) -> str:
    if kind == "mass_g":
        if value >= 1000:
            return f"{value / 1000:g} kg"
        return f"{value:g} g"
    if kind == "volume_ml":
        if value >= 1000:
            return f"{value / 1000:g} l"
        return f"{value:g} ml"
    if kind == "length_m":
        return f"{value:g} m"
    return f"{value:g} unidades"


def _quantities(text: str) -> list[str]:
    out: list[str] = []
    seen: set[tuple[str, float]] = set()
    for quantity in extract_quantities(text):
        key = (quantity.kind, quantity.value)
        if key in seen:
            continue
        seen.add(key)
        out.append(_quantity_label(*key))
    return out


def _with_decimal_comma(text: str) -> str:
    return re.sub(r"(?<=\d)\.(?=\d)", ",", text)


def _fresh_core(text: str) -> str:
    n = normalize_text(text)
    tokens = n.split()
    filtered = [t for t in tokens if t not in SEARCH_NOISE and not re.fullmatch(r"\d+(?:\.\d+)?", t)]
    # Remove unidades numéricas coladas que sobraram depois da normalização.
    filtered = [t for t in filtered if t not in {"g", "ml", "l", "m"}]
    return " ".join(filtered).strip()


def _synonym_queries(text: str) -> list[str]:
    n = normalize_text(text)
    out: list[str] = []
    for source, replacement in SEARCH_SYNONYMS:
        if source in n:
            out.append(n.replace(source, replacement))
        if replacement in n:
            out.append(n.replace(replacement, source))
    return out


def _brand_quantity_query(product: Product) -> str:
    if not product.marca:
        return ""
    quantities = _quantities(product.search_name)
    if not quantities:
        return product.marca
    return " ".join([product.marca, *quantities])


def _broad_fresh_query(product: Product) -> str:
    core = _fresh_core(product.search_name)
    tokens = core.split()
    if not tokens:
        return ""
    # Para hortifruti, o substantivo principal costuma ser suficiente para a
    # busca ampla; o matcher depois distingue variedade/alias.
    if normalize_text(product.categoria) == "hortifruti":
        return tokens[0]
    # Carnes/peixaria: preserva até três palavras para evitar misturar cortes.
    return " ".join(tokens[:3])


def build_search_queries(product: Product, store_key: str = "") -> list[str]:
    """Gera consultas em camadas, do nome exato ao fallback semântico.

    A ordem é deliberada: primeiro o produto pedido, depois aliases conhecidos,
    depois marca+atributos e por último uma busca ampla para categorias frescas.
    """
    queries: list[str] = []
    seen_keys: set[str] = set()

    def add(value: str) -> None:
        value = " ".join(str(value or "").split()).strip()
        key = normalize_text(value)
        if value and key and key not in seen_keys:
            seen_keys.add(key)
            queries.append(value)

    add(product.search_name)

    for alias in product.aliases_for_store(store_key):
        add(alias)

    # Vocabulário equivalente usado por catálogos diferentes, sem relaxar a
    # validação do matcher. Ex.: "refresco em pó" x "suco em pó".
    for synonym_query in _synonym_queries(product.search_name):
        add(_with_decimal_comma(synonym_query))

    focused_terms = []
    for term in product.termos_obrigatorios:
        normalized = normalize_text(term)
        if normalized in GENERIC_TERMS:
            continue
        focused_terms.append(term)
    focused = " ".join([product.marca, *focused_terms])
    add(_with_decimal_comma(focused))

    # Fallback de descoberta para produtos industrializados: marca + tamanho.
    # É propositalmente amplo, mas não torna o candidato válido sozinho; o
    # matcher continua exigindo identidade, tipo, marca e quantidade.
    category = normalize_text(product.categoria)
    if product.marca and category not in FRESH_CATEGORIES:
        add(_with_decimal_comma(_brand_quantity_query(product)))
        # A busca da Coocerqui às vezes só devolve o SKU correto ao consultar
        # a marca. O matcher permanece estrito e impede que esse fallback amplo
        # transforme outro produto da mesma marca em preço válido.
        add(product.marca)

    if product.termos_substituiveis:
        product_type = detect_product_type(product.search_name)
        generic = " ".join([product_type, product.marca, *_quantities(product.search_name)])
        add(_with_decimal_comma(generic))

    if category in FRESH_CATEGORIES or product.unidade_preco == "kg":
        core = _fresh_core(product.search_name)
        add(core)
        add(_broad_fresh_query(product))

    return queries[:9]
