from __future__ import annotations

from collections import defaultdict
from urllib.parse import urldefrag

from .guards import detect_product_type, flexible_variant_family
from .normalizer import normalize_text, quantity_signature
from .report import report_price

VALID_SOURCE_STATUSES = {"EXATO", "SUBSTITUTO ACEITO"}


def _canonical_url(value: str) -> str:
    return urldefrag(str(value or "").strip())[0].rstrip("/")


def _equivalent_variant_collision(grouped_items) -> bool:
    """Return True when one URL is a legitimate shared variant fallback.

    The same physical product page may validly serve more than one requested
    variant when the user policy allows substitution inside the same brand,
    product family and explicit quantity.  Example: two Coala 120 ml scents
    can both use a third Coala 120 ml scent when neither exact scent is found.
    This is *not* a blanket duplicate-URL exemption: brand, family, quantity,
    sale unit and observed price must all agree.
    """
    products = [item[1] for item in grouped_items]
    results = [item[2] for item in grouped_items]
    statuses = {result.source_match_status or result.status for result in results}
    if not statuses or not statuses.issubset(VALID_SOURCE_STATUSES):
        return False

    brands = {normalize_text(product.marca) for product in products}
    if len(brands) != 1 or not next(iter(brands), ""):
        return False

    requested_units = {str(product.unidade_preco or "") for product in products}
    if len(requested_units) != 1:
        return False

    signatures = [quantity_signature(product.search_name) for product in products]
    if not signatures or not signatures[0] or any(sig != signatures[0] for sig in signatures[1:]):
        return False

    families = {
        detect_product_type(product.search_name) or flexible_variant_family(product.search_name)
        for product in products
    }
    if len(families) != 1 or not next(iter(families), ""):
        return False

    prices = [report_price(result) for result in results]
    if not all(isinstance(price, (int, float)) for price in prices):
        return False
    return max(float(price) for price in prices) - min(float(price) for price in prices) <= 0.01


def _invalidate_collision(result, products: list[str]) -> None:
    result.status = "DIVERGENTE"
    result.definitive_price = None
    note = (
        "COLISÃO DE PRODUTO: a mesma URL foi associada a itens diferentes "
        f"({'; '.join(products)}); preço removido por segurança"
    )
    result.notes = (result.notes + "; " + note).strip("; ")


def suppress_store_duplicate_urls(items, store_key: str) -> list[dict]:
    """Remove colisões em uma sequência de pares ``(produto, resultado)``.

    Colisões entre variantes equivalentes podem compartilhar a URL quando
    marca, família, quantidade, unidade e preço são os mesmos. Fora dessa
    exceção explícita, se há exatamente uma correspondência EXATA e as demais
    são substituições, mantém somente a exata; demais colisões ambíguas são
    removidas.
    """
    alerts: list[dict] = []
    grouped = defaultdict(list)
    for row_index, (product, result) in enumerate(items):
        url = _canonical_url(result.url)
        if not url or not isinstance(report_price(result), (int, float)):
            continue
        grouped[url].append((row_index, product, result))

    for url, grouped_items in grouped.items():
        identities = {normalize_text(item[1].search_name) for item in grouped_items}
        if len(identities) <= 1:
            continue
        if _equivalent_variant_collision(grouped_items):
            # Shared URL is legitimate under the explicit substitution policy;
            # both rows keep the freshly re-read price.
            continue
        exact = [
            item for item in grouped_items
            if (item[2].source_match_status or item[2].status) == "EXATO"
        ]
        keep = {exact[0][0]} if len(exact) == 1 else set()
        names = [item[1].search_name for item in grouped_items]
        removed = []
        for row_index, product, result in grouped_items:
            if row_index in keep:
                continue
            _invalidate_collision(result, names)
            removed.append(product.search_name)
        alerts.append({
            "store": store_key,
            "url": url,
            "products": names,
            "kept": exact[0][1].search_name if len(exact) == 1 else None,
            "removed": removed,
        })
    return alerts


def suppress_duplicate_product_urls(records) -> list[dict]:
    """Aplica a proteção de colisão às duas lojas do relatório principal."""
    alerts: list[dict] = []
    for store_position, store_key in ((1, "coocerqui"), (2, "paguemenos")):
        alerts.extend(
            suppress_store_duplicate_urls(
                [(record[0], record[store_position]) for record in records],
                store_key,
            )
        )
    return alerts
